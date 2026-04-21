from __future__ import annotations

from .db_helpers import db_execute, db_fetch_one, write_preview_summary

import asyncio
import base64
import io
import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.main_util import TEXTAREA_MAX_BYTES
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
    git_commit,
    json,
    preview_page,
    run_details_page,
    run_details_test_fragment,
    run_execute,
    run_rejudge,
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
import app.impl.workspace.run_view_detail as run_view_detail_module
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.types import Kind


class TestUIRun(UIBaseSuite):
    class _FakeUpload:
        def __init__(self, data: bytes):
            self._buf = io.BytesIO(data)

        async def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        async def close(self) -> None:
            return None

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
        existing_row = config.verification_service.verification_record(verification_id)
        existing_metadata: dict[str, object] = {}
        existing_created_at = ""
        existing_finished_at = ""
        existing_signature = ""
        if existing_row is not None:
            payload = config.verification_service.verification_detail(verification_id)
            if isinstance(payload, dict):
                existing_metadata = dict(payload)
            existing_created_at = str(existing_row["created_at"] or "").strip()
            existing_finished_at = str(existing_row["finished_at"] or "").strip()
            existing_signature = str(existing_row["signature"] or "").strip()
        mode_token = "pass-fail"
        if isinstance(summary_extra, dict):
            mode_token = str(summary_extra.get("mode") or "").strip() or mode_token
        if isinstance(existing_metadata, dict) and existing_metadata:
            mode_token = str(existing_metadata.get("mode") or mode_token).strip() or mode_token
        runs_map: dict[str, object] = {}
        existing_runs_obj = existing_metadata.get("runs") if isinstance(existing_metadata, dict) else None
        if isinstance(existing_runs_obj, dict):
            runs_map = {str(k): dict(v) for k, v in existing_runs_obj.items() if isinstance(v, dict)}
        runs_order: list[str] = []
        existing_order_obj = existing_metadata.get("runs_order") if isinstance(existing_metadata, dict) else None
        if isinstance(existing_order_obj, list):
            runs_order = [str(item or "").strip() for item in existing_order_obj if str(item or "").strip()]
        source_paths: list[str] = []
        existing_paths_obj = existing_metadata.get("source_paths") if isinstance(existing_metadata, dict) else None
        if isinstance(existing_paths_obj, list):
            source_paths = [str(item or "").strip() for item in existing_paths_obj if str(item or "").strip()]
        signature = existing_signature or str(existing_metadata.get("signature") or "").strip()
        for item in runs:
            run_id = str(item.get("id") or "").strip()
            if not run_id:
                continue
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
                "artifact_path": str(item.get("artifact_path") or verification_root),
                "task_kind": str(item.get("task_kind") or "").strip(),
                "summary": summary_obj,
            }
            if run_id not in runs_order:
                runs_order.append(run_id)
        summary_extra_obj = dict(summary_extra or {})
        kind_token = str(kind or existing_metadata.get("kind") or Kind.ALL).strip() or Kind.ALL.value
        safe_build_id = (
            str(summary_extra_obj.get("artifact_verification_id") or "").strip()
            or str(existing_metadata.get("artifact_verification_id") or "").strip()
            or str(build_id or "").strip()
        )
        metadata = {
            "kind": kind_token,
            "mode": mode_token,
            "status": str(status or existing_metadata.get("status") or "").strip().lower() or "running",
            "verification_source": str(existing_metadata.get("verification_source") or "verification.start").strip() or "verification.start",
            "error": str(existing_metadata.get("error") or "").strip(),
            "updated_at": created_at,
            "finished_at": finished_at,
            "artifact_root": str(verification_root),
            "source_paths": source_paths,
            "artifact_verification_id": safe_build_id,
            "signature": signature,
            "runs_order": runs_order,
            "runs": runs_map,
            "tests": list(existing_metadata.get("tests") or []),
            "lifecycle": dict(existing_metadata.get("lifecycle") or {"steps": []}),
        }
        if summary_extra_obj:
            if str(summary_extra_obj.get("signature") or "").strip():
                signature = str(summary_extra_obj.get("signature") or "").strip()
            metadata.update(summary_extra_obj)
        metadata["signature"] = signature
        final_created_at = existing_created_at or created_at
        final_finished_at = finished_at or existing_finished_at
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            kind=kind_token,
            status=status,
            detail=metadata,
        )
        db_execute(
            "UPDATE verifications SET created_at=?, finished_at=? WHERE id=?",
            [final_created_at, final_finished_at or None, verification_id],
        )

    def _insert_stage_verification(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int,
        kind: str = Kind.ALL,
        signature: str = "",
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
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=str(signature or "").strip(),
            kind=str(kind or Kind.ALL).strip() or Kind.ALL.value,
            status=status,
            detail=summary_obj,
        )
        db_execute(
            "UPDATE verifications SET created_at=?, finished_at=? WHERE id=?",
            [created_at, finished_at, verification_id],
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
            inferred_kind = Kind.ALL.value
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
        self.assertIn("/problems/alice/sample/tests", add_manual_loc)
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

        page = ui_tests_page(_request("/problems/alice/sample/tests"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tests/spec.json", html)
        self.assertIn("tests/generator/002.in", html)
        self.assertIn("gen 99", html)
        self.assertIn('class="linkish danger-link" data-submit-form="1">Delete</a>', html)

    def test_tests_spec_edit_can_clear_sample_output_validate(self) -> None:
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
            sample="1",
            manual_input="1\n",
            sample_output="42\n",
            sample_output_validate=["0", "1"],
        )
        self.assertEqual(add_manual.status_code, 303)

        edit_spec = edit_spec_call(
            problem="alice/sample",
            user="alice",
            index="1",
            test_id="001",
            kind="manual",
            sample="1",
            payload="1\n",
            sample_output="42\n",
            sample_output_validate=["0"],
        )
        self.assertEqual(edit_spec.status_code, 303)

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual(len(tests), 1)
        self.assertFalse(bool(tests[0].get("sample_output_validate", True)))

        edit_spec_checked = edit_spec_call(
            problem="alice/sample",
            user="alice",
            index="1",
            test_id="001",
            kind="manual",
            sample="1",
            payload="1\n",
            sample_output="42\n",
            sample_output_validate=["0", "1"],
        )
        self.assertEqual(edit_spec_checked.status_code, 303)

        payload_checked = json.loads(spec_path.read_text(encoding="utf-8"))
        tests_checked = payload_checked.get("tests") or []
        self.assertEqual(len(tests_checked), 1)
        self.assertTrue(bool(tests_checked[0].get("sample_output_validate", True)))

        page = ui_tests_page(_request("/problems/alice/sample/tests"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('type="hidden" name="sample_output_validate" value="0"', html)

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
            gen_script_text="gen 10 1\r\ngen 30 3\r\n",
        )
        self.assertEqual(updated.status_code, 303)
        self.assertTrue(str(updated.headers.get("location", "")).endswith("/problems/alice/sample/tests"))

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual([(row.get("id"), row.get("kind")) for row in tests], [("001", "manual"), ("002", "gen"), ("003", "gen")])
        self.assertEqual((generator_dir / "002.in").read_text(encoding="utf-8"), "gen 10 1")
        self.assertEqual((generator_dir / "003.in").read_text(encoding="utf-8"), "gen 30 3")
        self.assertNotIn("\r", (generator_dir / "002.in").read_text(encoding="utf-8"))
        self.assertNotIn("\r", (generator_dir / "003.in").read_text(encoding="utf-8"))

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

        page = ui_tests_page(_request("/problems/alice/sample/tests"), "alice/sample", "alice")
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

        upload_payload = self._FakeUpload(b"7 8 9  \r\n10 11\t \r\n")
        uploaded = asyncio.run(
            upload_payload_call(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=upload_payload,
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertIn("/problems/alice/sample/tests", uploaded.headers.get("location", ""))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "7 8 9\n10 11\n")

        downloaded = download_payload_call(problem="alice/sample", user="alice", index="1")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("001.in", str(downloaded.headers.get("content-disposition", "")))

    def test_tests_spec_manual_payload_upload_accepts_payloads_larger_than_textarea_limit(self) -> None:
        oversized = (b"8" * (TEXTAREA_MAX_BYTES + 32)) + b"\r\n"
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

        uploaded = asyncio.run(
            upload_payload_call(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=self._FakeUpload(oversized),
            )
        )
        self.assertEqual(uploaded.status_code, 303)

        payload = (manual_dir / "001.in").read_text(encoding="utf-8")
        self.assertGreater(len(payload.encode("utf-8")), TEXTAREA_MAX_BYTES)
        self.assertTrue(payload.endswith("\n"))
        self.assertNotIn("\r", payload)

    def test_tests_spec_manual_payload_upload_rejects_non_utf8_payload(self) -> None:
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

        uploaded = asyncio.run(
            upload_payload_call(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=self._FakeUpload(b"\xff\xfe\xfd"),
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertIn("uploaded payload must be utf-8 text.", _flash_messages_from_response(uploaded))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "seed\n")

    def test_tests_spec_manual_payload_upload_uses_file_size_limit_not_textarea_limit(self) -> None:
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

        with patch("app.main_util.UPLOAD_MAX_BYTES", 8):
            uploaded = asyncio.run(
                upload_payload_call(
                    problem="alice/sample",
                    user="alice",
                    index="1",
                    payload_upload=self._FakeUpload(b"123456789"),
                )
            )
        self.assertEqual(uploaded.status_code, 303)
        self.assertIn("uploaded payload is too large.", _flash_messages_from_response(uploaded))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "seed\n")

    def test_tests_spec_add_manual_upload_route(self) -> None:
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

        upload = self._FakeUpload(b"11 22  \r\n33 44\t \r\n")
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
        self.assertIn("/problems/alice/sample/tests", location)
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

    def test_tests_spec_add_manual_upload_accepts_payloads_larger_than_textarea_limit(self) -> None:
        oversized = (b"9" * (TEXTAREA_MAX_BYTES + 32)) + b"\r\n"
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

        created = asyncio.run(
            add_manual_upload_call(
                problem="alice/sample",
                user="alice",
                test_id="",
                sample="0",
                manual_upload=self._FakeUpload(oversized),
            )
        )
        self.assertEqual(created.status_code, 303)

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("kind")), "manual")

        manual_text = (manual_dir / "001.in").read_text(encoding="utf-8")
        self.assertGreater(len(manual_text.encode("utf-8")), TEXTAREA_MAX_BYTES)
        self.assertTrue(manual_text.endswith("\n"))
        self.assertNotIn("\r", manual_text)

    def test_tests_spec_add_manual_upload_rejects_non_utf8_payload(self) -> None:
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

        created = asyncio.run(
            add_manual_upload_call(
                problem="alice/sample",
                user="alice",
                test_id="",
                sample="0",
                manual_upload=self._FakeUpload(b"\xff\xfe\xfd"),
            )
        )
        self.assertEqual(created.status_code, 303)
        self.assertIn("uploaded payload must be utf-8 text.", _flash_messages_from_response(created))
        self.assertFalse((manual_dir / "001.in").exists())

    def test_tests_spec_add_manual_upload_uses_file_size_limit_not_textarea_limit(self) -> None:
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

        with patch("app.main_util.UPLOAD_MAX_BYTES", 8):
            created = asyncio.run(
                add_manual_upload_call(
                    problem="alice/sample",
                    user="alice",
                    test_id="",
                    sample="0",
                    manual_upload=self._FakeUpload(b"123456789"),
                )
            )
        self.assertEqual(created.status_code, 303)
        self.assertIn("uploaded payload is too large.", _flash_messages_from_response(created))
        self.assertFalse((manual_dir / "001.in").exists())

    def test_tests_page_includes_templates_examples_and_mode_controls(self) -> None:
        add_manual_call(problem="alice/sample", user="alice", test_id="001", manual_input="1\n")
        page = ui_tests_page(_request("/problems/alice/sample/tests"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('data-popup-open="tests-add-manual-popup"', html)
        self.assertIn('data-popup-open="tests-upload-manual-popup"', html)
        self.assertIn('data-popup-open="tests-reindex-popup-1"', html)
        self.assertIn('action="/problems/alice/sample/tests/spec/gen-script"', html)
        self.assertRegex(
            html,
            r'<textarea[^>]*id="tests-gen-script-text"[^>]*data-code-editor="1"[^>]*data-code-path="tests/spec/gen-script\.txt"[^>]*data-code-height="220"[^>]*data-code-wrap="1"[^>]*>',
        )
        self.assertIn('action="/problems/alice/sample/tests/spec/reindex"', html)
        self.assertIn('action="/problems/alice/sample/tests/spec/add-manual-upload"', html)
        self.assertIn('class="tests-editor-table"', html)
        self.assertIn("<th>Test</th>", html)
        self.assertIn('data-sample-output-validate-group="1"', html)
        self.assertIn('data-sample-toggle="1"', html)
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
        def _fake_start_verification_job(*args, **kwargs):
            config.verification_service.begin_verification_record(
                verification_id=str(kwargs["verification_id"]),
                problem_id=problem_id,
                workspace_id=workspace_id,
                signature="deadbeef",
                kind=Kind.ALL.value,
                status="running",
                detail=dict(kwargs.get("initial_summary") or {}),
            )
            return True

        with patch("app.impl.run_export.run.start_verification_job", side_effect=_fake_start_verification_job):
            resp = run_execute(
                problem="alice/sample",
                user="alice",
                artifact_verification_id="",
                solution_paths=["solutions/accepted.cpp"],
                submission_upload=None,
            )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/run/details?verification_id=", loc)
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
        metadata = config.verification_service.verification_detail(verification_id)
        self.assertIsInstance(metadata, dict)
        self.assertEqual(str(metadata.get("mode") or ""), "pass-fail")

    def test_run_execute_uses_problem_mode_from_general_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        problem_cfg = ws / "config" / "problem.json"
        problem_cfg.parent.mkdir(parents=True, exist_ok=True)
        problem_cfg.write_text(json.dumps({"mode": "interactive", "pass_limit": 2}, indent=2) + "\n", encoding="utf-8")

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
        deadline = time.monotonic() + 8.0
        metadata: dict[str, object] = {}
        while time.monotonic() < deadline:
            metadata = config.verification_service.verification_detail(verification_id)
            if metadata:
                break
            time.sleep(0.05)
        self.assertTrue(metadata)
        self.assertEqual(str(metadata.get("mode") or ""), "interactive")

    def test_run_execute_records_verification_audit_before_queue_start(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        observed = {"checked": False}

        def _fake_start_verification_job(*args, **kwargs) -> bool:
            verification_id = str(kwargs.get("verification_id") or "")
            targets = list(kwargs.get("targets") or [])
            verification_run_ids = [str(item.get("run_id") or "") for item in targets if str(item.get("run_id") or "")]
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

        with patch("app.impl.run_export.run.start_verification_job", side_effect=_fake_start_verification_job):
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
        self.assertIn("/problems/alice/sample/run/details?verification_id=", loc)
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
        with patch("app.impl.run_export.run.start_verification_job", return_value=True) as start_batch:
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
        targets = kwargs.get("targets")
        self.assertIsInstance(targets, list)
        self.assertTrue(targets)
        first = targets[0]
        self.assertEqual(str(first.get("path") or ""), "solutions/accepted.cpp")
        self.assertEqual(str(first.get("expected_behavior") or ""), "accepted")

    def test_run_execute_uploaded_source_uses_task_graph_verification(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        class _FakeUpload:
            def __init__(self, filename: str, data: bytes):
                self.filename = filename
                self.file = io.BytesIO(data)

        upload = _FakeUpload("../tmp.cpp", b"int main(){return 0;}\n")
        with patch("app.impl.run_export.run.start_verification_job", return_value=True) as start_job:
            resp = run_execute(
                problem="alice/sample",
                user="alice",
                artifact_verification_id="",
                solution_paths=[],
                test_names=["001.in"],
                submission_upload=upload,
            )
        self.assertEqual(resp.status_code, 303)
        start_job.assert_called_once()
        kwargs = start_job.call_args.kwargs
        self.assertEqual(kwargs.get("selected_test_names"), ["001.in"])
        targets = list(kwargs.get("targets") or [])
        self.assertEqual(len(targets), 2)
        self.assertEqual(str(targets[0].get("path") or ""), "solutions/accepted.cpp")
        uploaded_target = targets[1]
        self.assertEqual(str(uploaded_target.get("upload_filename") or ""), "tmp.cpp")
        self.assertEqual(bytes(uploaded_target.get("upload_content") or b""), b"int main(){return 0;}\n")
        self.assertTrue(str(uploaded_target.get("path") or "").startswith("uploads/"))
        self.assertTrue(str(uploaded_target.get("path") or "").endswith("/tmp.cpp"))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("verification running", messages[0])

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

    def test_verification_sidebar_marks_stale_when_gen_chk_sol_tests_change(self) -> None:
        problem = f"alice/verify-stale-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        signature = workspace_impl._verification_sources_signature(ws)

        self._insert_verification_row(
            verification_id=f"ver-stale-{uuid.uuid4().hex[:8]}",
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verify-stale"),
            kind=Kind.ALL,
            status="ok",
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
            runs=[],
            summary_extra={
                "status": "pass",
                "signature": signature,
            },
        )

        (ws / "tests" / "manual" / "001.in").write_text("8\n", encoding="utf-8")

        page = general_page(_request(f"/problems/{problem}/statement"), problem, "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'<strong class="submenu-status-heading">Verification</strong>[\s\S]*?action="/problems/[^"]+/verification/start"[\s\S]*?<a\s+data-page="run"\s+class="submenu-detail-line problem-submenu-run-status submenu-status-warn"[^>]*>\s*stale\s*</a>',
        )
        self.assertRegex(
            html,
            r'<a\s+data-page="run"\s+class="submenu-detail-line problem-submenu-run-status submenu-status-warn"[^>]*data-tooltip="[^"]*changed: verification inputs[^"]*"[^>]*>\s*stale\s*</a>',
        )
        self.assertIn("changed: verification inputs", html)

    def test_verification_sidebar_marks_stale_when_general_info_changes(self) -> None:
        problem = f"alice/verify-stale-general-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        signature = workspace_impl._verification_sources_signature(ws)

        self._insert_verification_row(
            verification_id=f"ver-stale-general-{uuid.uuid4().hex[:8]}",
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verify-stale-general"),
            kind=Kind.ALL,
            status="ok",
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
            runs=[],
            summary_extra={
                "status": "pass",
                "signature": signature,
            },
        )

        problem_cfg = ws / "config" / "problem.json"
        payload: dict[str, object] = {}
        if problem_cfg.exists():
            payload = json.loads(problem_cfg.read_text(encoding="utf-8"))
        if "mode" not in payload:
            payload["mode"] = "pass-fail"
        if "pass_limit" not in payload:
            payload["pass_limit"] = 1
        payload["time_limit_ms"] = int(payload.get("time_limit_ms") or 2000) + 100
        problem_cfg.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        page = general_page(_request(f"/problems/{problem}/statement"), problem, "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'<strong class="submenu-status-heading">Verification</strong>[\s\S]*?action="/problems/[^"]+/verification/start"[\s\S]*?<a\s+data-page="run"\s+class="submenu-detail-line problem-submenu-run-status submenu-status-warn"[^>]*>\s*stale\s*</a>',
        )
        self.assertRegex(
            html,
            r'<a\s+data-page="run"\s+class="submenu-detail-line problem-submenu-run-status submenu-status-warn"[^>]*data-tooltip="[^"]*changed: verification inputs[^"]*"[^>]*>\s*stale\s*</a>',
        )
        self.assertIn("changed: verification inputs", html)

    def test_run_page_shows_multi_solution_selector_without_mode_select(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")

        page = run_new_page(_request("/problems/alice/sample/run/new", "solution_paths=solutions/wa.cpp"), "alice/sample", "alice")
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
        execute_html = html.split('<div id="statement-settings-popup"', 1)[0]
        self.assertNotIn("name=\"submission_path\"", execute_html)
        self.assertNotIn("name=\"mode\"", execute_html)
        self.assertIn("solutions/accepted.cpp", html)
        self.assertIn("solutions/wa.cpp", html)
        self.assertIn("value=\"solutions/wa.cpp\" checked", html)

    def test_rejudge_uses_verification_id_endpoint_and_forces_recompile(self) -> None:
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
            kind=Kind.ALL,
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
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-rerun-link-ok-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_ok,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                    "run_id": run_ok,
                },
                {
                    "id": f"vt-rerun-link-wa-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "logical_run_id": run_wa,
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_DONE,
                    "run_id": run_wa,
                },
            ],
            edges=[],
        )
        list_page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(list_page.status_code, 200)
        list_html = list_page.body.decode("utf-8", errors="replace")
        self.assertIn('method="post" action="/problems/alice/sample/run/rejudge"', list_html)
        self.assertIn(f'name="verification_id" value="{verification_id}"', list_html)
        self.assertNotIn("/run/new?rerun_verification_id=", list_html)

        details_page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(details_page.status_code, 200)
        detail_html = details_page.body.decode("utf-8", errors="replace")
        self.assertIn('method="post" action="/problems/alice/sample/run/rejudge"', detail_html)
        self.assertIn(f'name="verification_id" value="{verification_id}"', detail_html)
        self.assertNotIn('name="solution_paths"', detail_html)

        with patch("app.impl.run_export.run.start_verification_job", return_value=True) as start_job:
            response = run_rejudge("alice/sample", "alice", verification_id=verification_id)

        self.assertEqual(response.status_code, 303)
        call_kwargs = start_job.call_args.kwargs
        self.assertTrue(call_kwargs["force_recompile"])
        self.assertEqual(call_kwargs["selected_test_names"], [])
        self.assertEqual(
            [target["path"] for target in call_kwargs["targets"]],
            ["solutions/accepted.cpp", "solutions/wa.cpp"],
        )

    def test_run_page_defaults_all_tests_checked_when_available(self) -> None:
        problem = f"alice/run-default-tests-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
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
        page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("No verification yet.", html)
        self.assertNotIn("page-grid-wide", html)










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
                artifact_path=str(config.fs_manager.prepare_verification_root(verification_id).resolve()),
                created_at=created_at,
                finished_at=created_at,
                verification_id=verification_id,
                kind=Kind.ALL,
            )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=10, actor_user_id=int(ctx["user"]["id"]))
        ordered_ids = [str(item.get("id") or "") for item in rows]
        self.assertIn(old_verification, ordered_ids)
        self.assertIn(new_verification, ordered_ids)
        self.assertLess(ordered_ids.index(new_verification), ordered_ids.index(old_verification))
        old_row = next((item for item in rows if str(item.get("id") or "") == old_verification), {})
        self.assertEqual(str(old_row.get("created_at") or ""), "2026-03-03T00:00:00Z")

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
        run_ok_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_ok
        run_pending_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_pending
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
            kind=Kind.ALL,
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

        list_page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(list_page.status_code, 200)

        details_page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(details_page.status_code, 200)

    def test_run_cancel_marks_running_verification_failed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-cancel-{uuid.uuid4().hex[:8]}"
        run_id = f"r-cancel-running-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-cancel-run")
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
            status="running",
            created_at="2026-02-23T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={
                "mode": "pass-fail",
                "build_id": build_id,
                "task_graph": True,
                "source_paths": ["solutions/accepted.cpp"],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-cancel-leased",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_LEASED,
                    "run_id": run_id,
                    "judgehost_task_id": "jt-cancel-leased",
                },
                {
                    "id": "vt-cancel-pending",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )

        details_before = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(details_before.status_code, 200)

        cancel_resp = run_export_impl.run_cancel(problem="alice/sample", user="alice", verification_id=verification_id)
        self.assertEqual(cancel_resp.status_code, 303)
        self.assertIn(
            f"/problems/alice/sample/run/details?verification_id={verification_id}",
            str(cancel_resp.headers.get("location", "")),
        )
        cancel_messages = _flash_messages_from_response(cancel_resp)
        self.assertTrue(cancel_messages)
        self.assertIn("verification cancelled", cancel_messages[0])

        verification_row = db_fetch_one("SELECT status,finished_at FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or "").lower(), "failed")
        self.assertTrue(str(verification_row["finished_at"] or ""))
        rows = {
            str(row["id"]): row
            for row in VerificationTaskStore(config.db).list_rows(verification_id)
        }
        self.assertEqual(str(rows["vt-cancel-leased"]["status"] or ""), VerificationTaskStore.TASK_LEASED)
        self.assertEqual(str(rows["vt-cancel-pending"]["status"] or ""), VerificationTaskStore.TASK_CANCELLED)

        details_after = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(details_after.status_code, 200)

    def test_run_cancel_cancels_not_started_rows_without_active_judgehost_work(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-cancel-pending-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-cancel-pending")
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-05T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={"task_graph": True, "source_paths": ["solutions/accepted.cpp"]},
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-cancel-pending-1",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_QUEUED,
                    "run_id": f"r-cancel-pending-1-{uuid.uuid4().hex[:8]}",
                    "judgehost_task_id": "jt-cancel-pending-1",
                },
                {
                    "id": "vt-cancel-pending-2",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_QUEUED,
                    "run_id": f"r-cancel-pending-2-{uuid.uuid4().hex[:8]}",
                    "judgehost_task_id": "jt-cancel-pending-2",
                },
            ],
            edges=[],
        )
        cancel_resp = run_export_impl.run_cancel(problem="alice/sample", user="alice", verification_id=verification_id)
        self.assertEqual(cancel_resp.status_code, 303)
        cancel_messages = _flash_messages_from_response(cancel_resp)
        self.assertTrue(cancel_messages)
        self.assertIn("verification cancelled", cancel_messages[0])
        verification_row = db_fetch_one("SELECT status FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or "").strip().lower(), "failed")
        rows = VerificationTaskStore(config.db).list_rows(verification_id)
        self.assertEqual(
            [str(row["status"] or "") for row in rows],
            [VerificationTaskStore.TASK_CANCELLED, VerificationTaskStore.TASK_CANCELLED],
        )

    def test_run_cancel_cancels_queued_rows_when_domjudge_has_only_pending_cases(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-cancel-domjudge-pending-{uuid.uuid4().hex[:8]}"
        run_id = f"r-cancel-domjudge-pending-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-cancel-domjudge-pending")
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-05T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={"task_graph": True, "source_paths": ["solutions/accepted.cpp"]},
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-cancel-domjudge-pending-1",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_QUEUED,
                    "run_id": run_id,
                    "judgehost_task_id": "jt-cancel-domjudge-pending",
                },
                {
                    "id": "vt-cancel-domjudge-pending-2",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_QUEUED,
                    "run_id": run_id,
                    "judgehost_task_id": "jt-cancel-domjudge-pending",
                },
            ],
            edges=[],
        )
        with patch.object(
            config.judgehost_task_service,
            "domjudge_runs_with_leased_cases",
            return_value=set(),
        ):
            cancel_resp = run_export_impl.run_cancel(problem="alice/sample", user="alice", verification_id=verification_id)
        self.assertEqual(cancel_resp.status_code, 303)
        rows = VerificationTaskStore(config.db).list_rows(verification_id)
        self.assertEqual(
            [str(row["status"] or "") for row in rows],
            [VerificationTaskStore.TASK_CANCELLED, VerificationTaskStore.TASK_CANCELLED],
        )


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
            kind=Kind.ALL,
            status="failed",
            created_at="2026-03-14T00:00:00Z",
            finished_at="2026-03-14T00:00:10Z",
            runs=[
                {
                    "id": "r-main-failed",
                    "status": "failed",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "artifact_path": str(config.fs_manager.prepare_verification_root(verification_id).resolve()),
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

        page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(verification_id, html)
        self.assertIn(">failed<", html)
        self.assertNotIn(">running<", html)
        self.assertIn(">View</a>", html)
        self.assertNotIn(">Cancel</a>", html)

    def test_run_details_shows_task_status_for_verification(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-task-status-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-task-status-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-lifecycle")
        run_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 3,
            "usage": {"tests": 3},
        }
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
            status="running",
            created_at="2026-02-23T00:00:02Z",
            finished_at="",
            runs=[
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
                "verification_id": verification_id,
                "build_id": build_id,
                "verification_source": "verification.start",
                "execution_model": "task-dag",
                "task_graph": True,
                "solution_count": 1,
                "solutions": [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "run_id": run_id,
                        "verification_source": "verification.start",
                        "expected_behavior": "accepted",
                    }
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-done-1",
                    "task_kind": "generate-input",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "003.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-run-1",
                    "task_kind": "generate-input",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_LEASED,
                },
                {
                    "id": "vt-run-2",
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_LEASED,
                },
                {
                    "id": "vt-pending-1",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "004.in",
                    "expected_behavior": "accepted",
                    "queue_index": 4,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": "vt-pending-2",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "005.in",
                    "expected_behavior": "accepted",
                    "queue_index": 5,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": "vt-pending-3",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "006.in",
                    "expected_behavior": "accepted",
                    "queue_index": 6,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": "vt-pending-4",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "007.in",
                    "expected_behavior": "accepted",
                    "queue_index": 7,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": "vt-pending-5",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "008.in",
                    "expected_behavior": "accepted",
                    "queue_index": 8,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        detail_ctx = workspace_impl.build_run_detail_context(
            ctx,
            "pass-fail",
            requested_verification_id=verification_id,
        )
        self.assertEqual(dict(detail_ctx.get("detail_task_counts") or {}).get("pending"), 5)
        self.assertEqual(dict(detail_ctx.get("detail_task_counts") or {}).get("running"), 2)
        self.assertEqual(dict(detail_ctx.get("detail_task_counts") or {}).get("done"), 1)
        running_labels = [str(item.get("label") or "") for item in list(detail_ctx.get("detail_running_tasks") or [])]
        self.assertIn("Generate Input / accepted.cpp / 001.in", running_labels)
        self.assertIn("Main Correct / accepted.cpp / 002.in", running_labels)

    def test_run_details_running_issue_uses_info_note_and_status_moves_into_task_status(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-running-issue-{uuid.uuid4().hex[:8]}"
        run_id = f"r-running-issue-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-running-issue"),
            kind=Kind.ALL,
            status="running",
            created_at="2026-04-12T19:22:22Z",
            finished_at="",
            runs=[
                {
                    "id": run_id,
                    "status": "running",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests": [],
                        "tests_total": 1,
                    },
                }
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "task_counts": {
                    "total": 1,
                    "pending": 0,
                    "queued": 0,
                    "running": 1,
                    "done": 0,
                    "failed": 0,
                    "cancelled": 0,
                },
                "running_tasks": [
                    {
                        "task_id": "vt-running-issue-1",
                        "label": "Solution Run / wa.cpp / 001.in",
                    }
                ],
            },
        )
        db_execute(
            "UPDATE verifications SET fail_reason=? WHERE id=?",
            ["luangao.cpp: cancelled on service startup", verification_id],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("luangao.cpp: cancelled on service startup", html)

    def test_run_details_task_graph_shows_main_correct_columns(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-task-graph-{uuid.uuid4().hex[:8]}"
        main_run_id = f"r-main-{uuid.uuid4().hex[:8]}"
        solution_run_id = f"r-solution-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-graph"),
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-22T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": main_run_id,
                    "status": "ok",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests": [{"test": "001.in", "verdict": "AC", "feedback_files": []}],
                    },
                },
                {
                    "id": solution_run_id,
                    "status": "running",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests": [],
                        "tests_total": 1,
                    },
                },
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "execution_model": "task-dag",
                "task_graph": True,
                "solution_count": 1,
                "solutions": [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "run_id": main_run_id,
                        "verification_source": "verification.solve-main",
                        "expected_behavior": "accepted",
                    },
                    {
                        "source_path": "solutions/wa.cpp",
                        "run_id": solution_run_id,
                        "verification_source": "verification.start",
                        "expected_behavior": "wrong_answer",
                    },
                ],
                "task_counts": {
                    "total": 3,
                    "pending": 1,
                    "running": 1,
                    "done": 1,
                    "failed": 0,
                    "cancelled": 0,
                    "by_kind": {},
                },
                "running_tasks": [
                    {
                        "task_id": "vt-solution-1",
                        "label": "Solution run: wa.cpp / 001.in",
                    }
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-generate-1",
                    "task_kind": "generate-input",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-main-1",
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": main_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-solution-1",
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "logical_run_id": solution_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_LEASED,
                },
            ],
            edges=[
                ("vt-generate-1", "vt-main-1"),
                ("vt-main-1", "vt-solution-1"),
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("wa.cpp", html)
        self.assertIn("accepted.cpp", html)
        self.assertIn('data-solution-title="wa.cpp"', html)
        self.assertIn('data-test-name="001.in"', html)
        self.assertIn("running", html)

    def test_run_details_task_graph_ignores_stale_summary_test_cells(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-task-stale-summary-{uuid.uuid4().hex[:8]}"
        solution_run_id = f"r-stale-summary-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-stale-summary"),
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-23T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": solution_run_id,
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "task_kind": "solution-run",
                        "tests_total": 2,
                        "tests": [
                            {"test": "002.in", "verdict": "WA", "time_ms": 9999, "memory_kb": 8192, "feedback_files": []},
                        ],
                    },
                },
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "execution_model": "task-dag",
                "task_graph": True,
                "solution_count": 1,
                "solutions": [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "run_id": solution_run_id,
                        "task_kind": "solution-run",
                        "expected_behavior": "accepted",
                    },
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-generate-001",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-generate-002",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-solution-001",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": solution_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "AC",
                    "runtime_sec": 0.001,
                    "cpu_sec": 0.001,
                    "wall_sec": 0.001,
                    "memory_kb": 1024,
                },
                {
                    "id": "vt-solution-002",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": solution_run_id,
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 4,
                    "status": VerificationTaskStore.TASK_QUEUED,
                },
            ],
            edges=[],
        )
        detail_ctx = workspace_impl.build_run_detail_context(
            workspace_service.workspace_context("alice/sample", "alice", include_recent=False),
            "pass-fail",
            requested_verification_id=verification_id,
        )
        columns = {
            str(col.get("title") or ""): col
            for col in list(detail_ctx.get("detail_columns") or [])
        }
        accepted_col = dict(columns["accepted.cpp"])
        tests_map = dict(accepted_col.get("tests_map") or {})
        self.assertEqual(str(accepted_col.get("status") or ""), "queued")
        self.assertEqual(str(tests_map["001.in"]["short"] or ""), "AC")
        self.assertNotIn("002.in", tests_map)
        self.assertNotIn("003.in", tests_map)

    def test_run_details_task_graph_shows_generate_status_from_task_rows(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-task-generate-{uuid.uuid4().hex[:8]}"
        solution_run_id = f"r-solution-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-generate"),
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-22T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": solution_run_id,
                    "status": "running",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests": [{"test": "001.in", "verdict": "--", "feedback_files": []}],
                        "tests_total": 1,
                    },
                },
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "execution_model": "task-dag",
                "task_graph": True,
                "solution_count": 1,
                "solutions": [
                    {
                        "source_path": "solutions/wa.cpp",
                        "run_id": solution_run_id,
                        "verification_source": "verification.start",
                        "expected_behavior": "wrong_answer",
                    },
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-generate-1",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_LEASED,
                },
                {
                    "id": "vt-solution-1",
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "logical_run_id": solution_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[("vt-generate-1", "vt-solution-1")],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("running 1", html)
        self.assertIn('title="001.in"', html)
        self.assertIn("generating", html)

    def test_run_details_task_graph_shows_running_only_for_running_solution_test(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-task-solution-status-{uuid.uuid4().hex[:8]}"
        solution_run_id = f"r-solution-status-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-solution-status"),
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-23T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": solution_run_id,
                    "status": "running",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "task_kind": "solution-run",
                        "tests_total": 2,
                    },
                },
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "execution_model": "task-dag",
                "task_graph": True,
                "solution_count": 1,
                "solutions": [
                    {
                        "source_path": "solutions/wa.cpp",
                        "run_id": solution_run_id,
                        "task_kind": "solution-run",
                        "expected_behavior": "wrong_answer",
                    },
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-generate-001",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-generate-002",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-solution-001",
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "logical_run_id": solution_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_LEASED,
                },
                {
                    "id": "vt-solution-002",
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "logical_run_id": solution_run_id,
                    "test_name": "002.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 4,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("001.in", html)
        self.assertIn("002.in", html)
        self.assertIn("running", html)
        self.assertIn('class="vcell tone-running"', html)
        self.assertNotIn('class="vmeta">pending</span>', html)

    def test_run_details_task_graph_keeps_cancelled_solution_columns_visible_after_failed_cancel(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "cancelled.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-task-cancelled-column-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-accepted-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-cancelled-column"),
            kind=Kind.ALL,
            status="failed",
            created_at="2026-03-23T00:00:00Z",
            finished_at="2026-03-23T00:00:10Z",
            runs=[
                {
                    "id": accepted_run_id,
                    "status": "ok",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "task_kind": "solution-run",
                        "tests": [{"test": "001.in", "verdict": "AC", "feedback_files": []}],
                        "tests_total": 2,
                    },
                },
            ],
            summary_extra={
                "status": "failed",
                "error": "verification cancelled",
                "verification_id": verification_id,
                "execution_model": "task-dag",
                "task_graph": True,
                "solution_count": 2,
                "solutions": [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "run_id": accepted_run_id,
                        "task_kind": "solution-run",
                        "expected_behavior": "accepted",
                    },
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-generate-001",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-generate-002",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-accepted-001",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": accepted_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-accepted-002",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": accepted_run_id,
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 4,
                    "status": VerificationTaskStore.TASK_CANCELLED,
                },
                {
                    "id": "vt-cancelled-001",
                    "task_kind": "solution-run",
                    "source_path": "solutions/cancelled.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 5,
                    "status": VerificationTaskStore.TASK_CANCELLED,
                },
                {
                    "id": "vt-cancelled-002",
                    "task_kind": "solution-run",
                    "source_path": "solutions/cancelled.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 6,
                    "status": VerificationTaskStore.TASK_CANCELLED,
                },
            ],
            edges=[],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("cancelled.cpp", html)
        self.assertIn("cancelled", html)
        detail_ctx = workspace_impl.build_run_detail_context(
            workspace_service.workspace_context("alice/sample", "alice", include_recent=False),
            "pass-fail",
            requested_verification_id=verification_id,
        )
        columns = {
            str(col.get("title") or ""): col
            for col in list(detail_ctx.get("detail_columns") or [])
        }
        cancelled_col = dict(columns["cancelled.cpp"])
        self.assertEqual(str(cancelled_col.get("got_short") or ""), "--")
        self.assertEqual(str(cancelled_col.get("result_kind") or ""), "neutral")
        self.assertEqual(str(cancelled_col.get("result_tone_class") or ""), "tone-neutral")

    def test_run_details_show_cancelled_main_correct_cells_for_failed_verification(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "cancelled.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-main-cancel-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-main-cancel"),
            kind=Kind.ALL,
            status="failed",
            created_at="2026-03-24T00:00:00Z",
            finished_at="2026-03-24T00:00:05Z",
            runs=[],
            summary_extra={
                "mode": "pass-fail",
                "task_graph": True,
                "source_paths": ["solutions/accepted.cpp", "solutions/cancelled.cpp"],
                "status": "failed",
                "error": "verification cancelled by user",
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-generate-001",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-generate-002",
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_DONE,
                },
                {
                    "id": "vt-main-001",
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_CANCELLED,
                },
                {
                    "id": "vt-main-002",
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 4,
                    "status": VerificationTaskStore.TASK_CANCELLED,
                },
                {
                    "id": "vt-solution-001",
                    "task_kind": "solution-run",
                    "source_path": "solutions/cancelled.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 5,
                    "status": VerificationTaskStore.TASK_CANCELLED,
                },
                {
                    "id": "vt-solution-002",
                    "task_kind": "solution-run",
                    "source_path": "solutions/cancelled.cpp",
                    "logical_run_id": "",
                    "test_name": "002.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 6,
                    "status": VerificationTaskStore.TASK_CANCELLED,
                },
            ],
            edges=[],
        )
        detail_ctx = workspace_impl.build_run_detail_context(
            workspace_service.workspace_context("alice/sample", "alice", include_recent=False),
            "pass-fail",
            requested_verification_id=verification_id,
        )
        columns = {
            str(col.get("title") or ""): col
            for col in list(detail_ctx.get("detail_columns") or [])
        }
        main_col = dict(columns["accepted.cpp"])
        self.assertEqual(str(main_col.get("got_short") or ""), "--")
        self.assertEqual(str(main_col.get("result_kind") or ""), "neutral")
        rows = list(detail_ctx.get("detail_rows") or [])
        self.assertEqual(str(rows[0]["cells"][0]["metrics"] or ""), "cancelled")
        self.assertEqual(str(rows[1]["cells"][0]["metrics"] or ""), "cancelled")

    def test_statement_sidebar_shows_running_for_task_graph_verification(self) -> None:
        problem = f"alice/sidebar-running-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        commit_resp = git_commit(problem=problem, user="alice", message=f"sidebar-running-{uuid.uuid4().hex[:6]}")
        self.assertEqual(commit_resp.status_code, 303)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-sidebar-running-{uuid.uuid4().hex[:8]}"
        solution_run_id = f"r-sidebar-running-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-sidebar-running"),
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-22T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": solution_run_id,
                    "status": "running",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests": [],
                        "tests_total": 1,
                    },
                }
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "execution_model": "task-dag",
                "task_graph": True,
                "solutions": [
                    {
                        "source_path": "solutions/wa.cpp",
                        "run_id": solution_run_id,
                        "verification_source": "verification.start",
                        "expected_behavior": "wrong_answer",
                        "matched": False,
                        "completed": False,
                        "passed_all_tests": False,
                        "reason": "",
                    }
                ],
                "task_counts": {
                    "total": 3,
                    "pending": 1,
                    "running": 1,
                    "done": 1,
                    "failed": 0,
                    "cancelled": 0,
                    "by_kind": {},
                },
                "running_tasks": [
                    {
                        "task_id": "vt-solution-1",
                        "label": "Solution run: wa.cpp / 001.in",
                    }
                ],
            },
        )
        page = general_page(_request(f"/problems/{problem}/statement"), problem, "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'<strong class="submenu-status-heading">Verification</strong>[\s\S]*?<a\s+data-page="run"\s+class="submenu-detail-line problem-submenu-run-status [^"]*"[^>]*>\s*running\s*</a>',
        )

    def test_verification_start_shows_running_on_first_statement_render(self) -> None:
        problem = f"alice/verify-running-sidebar-{uuid.uuid4().hex[:8]}"
        self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        workspace_key = workspace_context_job._verification_workspace_key(problem_id, workspace_id)

        class _FakeWorker:
            def __init__(self) -> None:
                self._alive = True

            def is_alive(self) -> bool:
                return self._alive

            def stop(self) -> None:
                self._alive = False

        fake_worker = _FakeWorker()
        try:
            with patch.object(
                config.worker_queue_service,
                "submit",
                return_value=(fake_worker, True, "queued"),
            ):
                start_resp = verification_start(problem=problem, user="alice", page="statement")
            self.assertEqual(start_resp.status_code, 303)
            page = general_page(_request(f"/problems/{problem}/statement"), problem, "alice")
            self.assertEqual(page.status_code, 200)
            html = page.body.decode("utf-8", errors="replace")
            self.assertRegex(
                html,
                r'<strong class="submenu-status-heading">Verification</strong>[\s\S]*?<a\s+data-page="run"\s+class="submenu-detail-line problem-submenu-run-status [^"]*"[^>]*>\s*running\s*</a>',
            )
            row = config.verification_service.list_workspace_verification_rows(
                problem_id,
                workspace_id,
                limit=1,
            )
            self.assertIsNotNone(row)
            assert row
            self.assertEqual(str(row[0]["status"] or ""), "running")
        finally:
            fake_worker.stop()
            with config.verification_lock:
                config.verification_inflight.discard(workspace_key)
                config.verification_workers.discard(fake_worker)

    def test_run_details_ignores_runner_build_step_log_entries_when_rendering_verification_lifecycle(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-step-shape-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-step-shape")
        build_root = Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
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
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Generate Inputs", html)
        self.assertNotIn("Generate Outputs", html)
        self.assertNotIn("Run Solutions", html)
        self.assertNotIn("Check Expectations", html)
        self.assertNotIn('verification-step-tab-title">Compile<', html)
        self.assertNotIn('verification-step-tab-title">Solve<', html)
        self.assertNotIn("Verification Progress", html)

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
        build_root = Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
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
        run_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_id
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
            signature="deadbeef",
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
            kind=Kind.ALL,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "verification_id": verification_id,
                        "note": "audit payload intentionally ignored",
                    }
                ),
                "2026-02-23T00:00:03Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Generated tests", html)
        self.assertNotIn("Validated inputs", html)
        self.assertNotIn("Generate Outputs", html)
        self.assertNotIn("Run Solutions", html)

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
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="deadbeef",
            status="running",
            summary={"selected_tests": ["001.in", "002.in"], "selected_tests_count": 2},
            created_at="2026-03-03T00:00:00Z",
            finished_at=None,
        )
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
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
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Verification Progress", html)
        self.assertNotIn("Generate Inputs", html)
        self.assertNotIn("Generate Outputs", html)
        self.assertNotIn("generating inputs", html)
        self.assertNotIn("Generated tests", html)
        self.assertNotIn("2 tests", html)
        self.assertNotIn("Waiting for input-stage testcase results.", html)

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
        run_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_id
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
            kind=Kind.ALL,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "verification_id": verification_id,
                        "note": "audit payload intentionally ignored",
                    }
                ),
                "2026-03-04T00:00:01Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Generate Outputs", html)
        self.assertNotIn("Completed", html)
        self.assertNotIn("Waiting for validation results.", html)

    def test_run_details_uses_top_level_error_when_verification_fails_before_any_stage_starts(self) -> None:
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
            kind=Kind.ALL,
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
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("sqlite3.Row", html)
        self.assertIn("attribute &#39;get&#39;", html)
        self.assertNotIn("output generation failed", html)
        self.assertNotIn("Verification Progress", html)
        self.assertIn("<h2>Diagnostics</h2>", html)

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
        build_root = Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="deadbeef",
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
            kind=Kind.ALL,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "verification_id": verification_id,
                        "note": "audit payload intentionally ignored",
                    }
                ),
                "2026-02-23T00:00:04Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Run Solutions", html)
        self.assertIn('verification-task-status-state danger">failed</span>', html)
        self.assertNotIn("failed (1/1 completed)", html)
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
        build_root = Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="deadbeef",
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
            kind=Kind.ALL,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "verification_id": verification_id,
                        "note": "audit payload intentionally ignored",
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
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Run Solutions", html)
        self.assertIn('verification-task-status-state danger">failed</span>', html)
        self.assertNotIn("failed (1/1 completed)", html)
        self.assertNotIn("Cancelled solutions", html)
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
            kind=Kind.ALL,
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
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Verification Progress", html)
        self.assertNotIn("Generate Inputs", html)
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
            kind=Kind.ALL,
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
            kind=Kind.ALL,
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
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Verification Progress", html)
        self.assertNotIn("Generate Inputs", html)

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
        build_root = Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="deadbeef",
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
            kind=Kind.ALL,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "verification_id": verification_id,
                        "note": "audit payload intentionally ignored",
                    }
                ),
                "2026-02-23T00:00:04Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Execution Status", html)
        self.assertNotIn("Run Solutions", html)
        self.assertRegex(html, r"build failed:\s*compare script\s+\d+\s+crashed with exit code\s+1")
        self.assertNotIn("Check Expectations", html)

    def test_run_details_uses_default_sidebar_without_detail_table(self) -> None:
        page = run_details_page(_request("/problems/alice/sample/run/details"), "alice/sample", "alice")
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

    def test_run_details_transcript_preview_shows_download_link(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        token = "cache://judgehost-domjudge-case/" + ("a" * 64) + "/" + ("b" * 64) + "/program.out"
        encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
        download_href = f"/problems/alice/sample/artifacts/ver-r-transcript/blob/{encoded}/program.out"
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
                                        "time_display": "1ms (2ms wall)",
                                        "status_display": "AC · 1ms (2ms wall) · 1MB",
                                        "memory_display": "1MB",
                                        "feedback_display": "ok",
                                        "output_preview": {
                                        "available": True,
                                        "text": "> ping\n< pong\n",
                                        "truncated": False,
                                        "limit": 1024,
                                        "download_href": download_href,
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
                _request("/problems/alice/sample/run/details/test-fragment", "verification_id=ver-r-transcript&test=001.in"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("Transcript (first 2 lines)", detail_html)
        self.assertIn(download_href, detail_html)
        self.assertIn(">download</a>", detail_html)

    def test_run_cell_kind_nonaccepted_expected_uses_required_allowed_policy(self) -> None:
        self.assertEqual(workspace_impl._run_cell_kind("OK", "wrong_answer"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "wrong_answer"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "wrong_answer"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "run_time_error"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "run_time_error"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "run_time_error"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "time_limit_exceeded"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "time_limit_exceeded"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "time_limit_exceeded"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "tle_or_correct"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "tle_or_correct"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "tle_or_correct"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "tle_or_re"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("RE", "tle_or_re"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "tle_or_re"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("AC", "tle_or_re"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("FL", "wrong_answer"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("FL", "unknown"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "rejected"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "accepted"), "ok")

    def test_run_details_keeps_silent_ac_plain_text(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-unexpected-ac-text-{uuid.uuid4().hex[:8]}"
        run_id = f"r-unexpected-ac-text-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-unexpected-ac-text"),
            kind=Kind.ALL,
            status="ok",
            created_at="2026-03-24T00:00:00Z",
            finished_at="2026-03-24T00:00:01Z",
            runs=[
                {
                    "id": run_id,
                    "status": "ok",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "task_kind": "solution-run",
                        "expected_behavior": "wrong_answer",
                        "tests": [{"test": "001.in", "verdict": "OK", "time_ms": 1, "memory_kb": 0}],
                        "tests_total": 1,
                    },
                },
            ],
            summary_extra={
                "status": "ok",
                "verification_id": verification_id,
                "execution_model": "task-dag",
                "task_graph": True,
                "solution_count": 1,
                "solutions": [
                    {
                        "source_path": "solutions/wa.cpp",
                        "run_id": run_id,
                        "task_kind": "solution-run",
                        "expected_behavior": "wrong_answer",
                    },
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-unexpected-ac-text-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "logical_run_id": run_id,
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "runtime_sec": 0.001,
                    "cpu_sec": 0.001,
                    "wall_sec": 0.001,
                    "memory_kb": 0,
                    "run_id": run_id,
                },
            ],
            edges=[],
        )
        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("verification-detail-unexpected", html)
        self.assertIn('<span class="vcode">AC</span>', html)
        self.assertIn('<span class="vcode vcode-unexpected">AC</span>', html)

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

    def test_run_details_uses_diagnostics_heading(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        run_id = f"r-diag-heading-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-diag-heading")
        run_root = Path(os.environ["POLYGON_REPLICA_RUN_ROOT"]) / run_id
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
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("<h2>Diagnostics</h2>", html)
        self.assertNotIn("Compile Diagnostics", html)

    def test_run_detail_failure_reason_rewrites_generic_reason_with_source(self) -> None:
        from app.impl.workspace.run_view_detail import _rewrite_failure_reason_with_source

        generic_reason = "required=[AC], allowed=[AC], got=[TL]"
        reason = _rewrite_failure_reason_with_source(
            generic_reason,
            [
                {
                    "source": "solutions/ac_python.py",
                    "match_reason": generic_reason,
                    "error": "",
                }
            ],
        )
        self.assertEqual(reason, "ac_python.py: required=[AC], allowed=[AC], got=[TL]")

    def test_verification_match_omits_rule_reason_for_incomplete_solution_run(self) -> None:
        matched, completed, observed_pass, reason = workspace_impl._verification_solution_match(
            "rejected",
            "failed",
            {"error": "cancelled on service startup"},
        )
        self.assertFalse(matched)
        self.assertFalse(completed)
        self.assertFalse(observed_pass)
        self.assertEqual(reason, "")

    def test_run_detail_failure_reason_rewrites_incomplete_reason_with_column_error(self) -> None:
        from app.impl.workspace.run_view_detail import _rewrite_failure_reason_with_source

        generic_reason = "required=[WA, TL, RE, CE], allowed=[AC, WA, TL, RE, CE], got=[]: cancelled on service startup"
        reason = _rewrite_failure_reason_with_source(
            generic_reason,
            [
                {
                    "source": "solutions/luangao.cpp",
                    "match_reason": "",
                    "error": "cancelled on service startup",
                }
            ],
        )
        self.assertEqual(reason, "luangao.cpp: cancelled on service startup")

    def test_run_detail_failure_reason_does_not_promote_transient_running_state(self) -> None:
        from app.impl.workspace.run_view_detail import _rewrite_failure_reason_with_source

        reason = _rewrite_failure_reason_with_source(
            "",
            [
                {
                    "source": "solutions/std.cpp",
                    "match_reason": "running",
                    "error": "",
                }
            ],
        )
        self.assertEqual(reason, "")

    def test_run_details_reads_runtime_inputs_answers_and_column_outputs_for_task_graph(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "tmp.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        verification_id = f"ver-runtime-detail-{uuid.uuid4().hex[:8]}"
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            status="ok",
            detail={"status": "ok"},
        )
        input_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="input",
            file_name="001.in",
            payload=b"1 2 3\n",
        )
        answer_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="answer",
            file_name="001.ans",
            payload=b"6\n",
        )
        output_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="output",
            file_name="001.out",
            payload=b"6\n",
            extra_tags={"run_id": "tmp.cpp"},
        )
        config.verification_service.update_verification_artifact_refs(
            verification_id,
            "001.in",
            {"input_ref": input_ref, "answer_ref": answer_ref},
        )

        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-runtime-detail-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/tmp.cpp",
                    "logical_run_id": "tmp.cpp",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "runtime_sec": 0.003,
                    "cpu_sec": 0.002,
                    "wall_sec": 0.003,
                    "memory_kb": 1024,
                    "compile_log": "",
                    "diagnostics_json": "[]",
                    "error_text": "",
                    "feedback_text": "",
                    "output_ref": output_ref,
                }
            ],
            edges=[],
        )

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertRegex(detail_html, r"(?s)<strong>Input 001\.in</strong>.*?<pre[^>]*>\s*1 2 3\s*</pre>")
        self.assertRegex(detail_html, r"(?s)<strong>Answer</strong>.*?<pre[^>]*>\s*6\s*</pre>")
        self.assertNotIn("<strong>Generation</strong>", detail_html)
        self.assertIn(
            f"/problems/alice/sample/artifacts/{verification_id}/tests/001.in",
            detail_html,
        )
        self.assertIn(
            f"/problems/alice/sample/artifacts/{verification_id}/ans/001.ans",
            detail_html,
        )
        self.assertIn(f"/problems/alice/sample/artifacts/{verification_id}/output/", detail_html)
        self.assertIn("/001.out", detail_html)
        self.assertNotIn("(output file missing)", detail_html)
        self.assertNotIn(">missing<", detail_html)
        input_download = run_export_impl.artifact_file(
            "alice/sample",
            "alice",
            verification_id,
            "tests/001.in",
        )
        self.assertEqual(input_download.status_code, 200)
        self.assertEqual(input_download.body, b"1 2 3\n")
        answer_download = run_export_impl.artifact_file(
            "alice/sample",
            "alice",
            verification_id,
            "ans/001.ans",
        )
        self.assertEqual(answer_download.status_code, 200)
        self.assertEqual(answer_download.body, b"6\n")

    def test_run_test_detail_fragment_hides_ok_generation_validation_details(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "generators").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "tmp.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (workspace / "generators" / "random_tree.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        verification_id = f"ver-generate-detail-{uuid.uuid4().hex[:8]}"
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            status="ok",
            detail={
                "status": "ok",
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "source": "generators/random_tree.cpp",
                        "command": "random_tree 10 20",
                    }
                ],
            },
        )
        input_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="input",
            file_name="001.in",
            payload=b"1 2 3\n",
        )
        answer_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="answer",
            file_name="001.ans",
            payload=b"6\n",
        )
        output_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="output",
            file_name="001.out",
            payload=b"6\n",
            extra_tags={"run_id": "tmp.cpp"},
        )
        config.verification_service.update_verification_artifact_refs(
            verification_id,
            "001.in",
            {"input_ref": input_ref, "answer_ref": answer_ref},
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-generate-detail-{uuid.uuid4().hex[:8]}",
                    "task_kind": "generate-input",
                    "source_path": "generators/random_tree.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "AC",
                    "runtime_sec": 0.007,
                    "cpu_sec": 0.006,
                    "wall_sec": 0.007,
                    "memory_kb": 2048,
                    "compile_log": "",
                    "diagnostics_json": "[]",
                    "error_text": "",
                    "feedback_text": "tree is valid",
                    "output_ref": "",
                },
                {
                    "id": f"vt-runtime-generate-detail-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/tmp.cpp",
                    "logical_run_id": "tmp.cpp",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "runtime_sec": 0.003,
                    "cpu_sec": 0.002,
                    "wall_sec": 0.003,
                    "memory_kb": 1024,
                    "compile_log": "",
                    "diagnostics_json": "[]",
                    "error_text": "",
                    "feedback_text": "",
                    "output_ref": output_ref,
                },
            ],
            edges=[],
        )

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertNotIn("Generation of 001.in", detail_html)
        self.assertNotIn("generation-metrics", detail_html)
        self.assertNotIn("tree is valid", detail_html)
        self.assertNotIn("<th>Source</th>", detail_html)
        self.assertNotIn("<th>Command</th>", detail_html)
        self.assertNotIn("<th>Validator</th>", detail_html)
        self.assertRegex(detail_html, r"(?s)<table class=\"sol-metrics\">.*?<th>Status</th>.*?<th>Feedback</th>")
        self.assertIn('<span class="vmeta">2ms (3ms wall)</span>', detail_html)
        self.assertIn('<span class="vmeta">1MB</span>', detail_html)

    def test_generation_status_text_uses_generation_specific_labels(self) -> None:
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_DONE, "AC"), "OK")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_DONE, "OK"), "OK")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_FAILED, "WA"), "validation failed")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_FAILED, "TL"), "generator TL")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_FAILED, "TLX"), "generator TL")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_FAILED, "RE"), "generator RE")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_FAILED, "CE"), "generator CE")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_FAILED, "FL"), "validator failed")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_PENDING, "WA"), "pending")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_LEASED, "WA"), "running")
        self.assertEqual(run_view_detail_module._generation_status_text(VerificationTaskStore.TASK_CANCELLED, "WA"), "cancelled")

    def test_run_details_page_keeps_test_popup_available_for_generate_stage_failure(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "generators").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "tmp.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (workspace / "generators" / "random_tree.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        verification_id = f"ver-generate-fail-{uuid.uuid4().hex[:8]}"
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            status="failed",
            detail={
                "status": "failed",
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "source": "generators/random_tree.cpp",
                        "command": "random_tree 10 20",
                    }
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-generate-fail-{uuid.uuid4().hex[:8]}",
                    "task_kind": "generate-input",
                    "source_path": "generators/random_tree.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_FAILED,
                    "verdict": "FL",
                    "runtime_sec": 0.004,
                    "cpu_sec": 0.004,
                    "wall_sec": 0.004,
                    "memory_kb": 1536,
                    "compile_log": "",
                    "diagnostics_json": "[]",
                    "error_text": "validator rejected generated test",
                    "feedback_text": "",
                    "output_ref": "",
                },
                {
                    "id": f"vt-runtime-pending-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/tmp.cpp",
                    "logical_run_id": "tmp.cpp",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )

        page = run_details_page(_request("/problems/alice/sample/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            page_html,
            r'(?s)<td class="tcell tone-fail"[^>]*>\s*<a href="#run-test-detail-popup" data-popup-open="run-test-detail-popup" data-test-name="001\.in">001\.in</a>',
        )

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("<strong>Generation of 001.in: random_tree 10 20</strong>", detail_html)
        self.assertRegex(detail_html, r"(?s)<table class=\"sol-metrics generation-metrics\">.*?<th>Status</th>.*?<th>Feedback</th>")
        self.assertRegex(detail_html, r"(?s)<td class=\"status-cell tone-fail\">.*?<span class=\"vcode\">validator failed</span>")
        self.assertRegex(detail_html, r"(?s)<td class=\"fb-cell\">-</td>")
        self.assertIn("Error", detail_html)
        self.assertIn("validator rejected generated test", detail_html)

    def test_run_test_detail_fragment_hides_manual_validate_placeholder_source(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])

        verification_id = f"ver-manual-generate-{uuid.uuid4().hex[:8]}"
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            status="ok",
            detail={
                "status": "ok",
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "kind": "manual",
                        "source": "manual_validate.cpp",
                    }
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-manual-generate-{uuid.uuid4().hex[:8]}",
                    "task_kind": "generate-input",
                    "source_path": "manual_validate.cpp",
                    "logical_run_id": "",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "AC",
                    "runtime_sec": 0.001,
                    "cpu_sec": 0.001,
                    "wall_sec": 0.001,
                    "memory_kb": 256,
                    "compile_log": "",
                    "diagnostics_json": "[]",
                    "error_text": "",
                    "feedback_text": "manual input valid",
                    "output_ref": "",
                }
            ],
            edges=[],
        )

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertNotIn("Generation of 001.in", detail_html)
        self.assertNotIn("manual_validate.cpp", detail_html)
        self.assertNotIn("<th>Command</th>", detail_html)
        self.assertNotIn("<th>Source</th>", detail_html)

    def test_run_details_page_shows_main_correct_compile_diagnostics_text(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "std.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        verification_id = f"ver-main-correct-diag-{uuid.uuid4().hex[:8]}"
        detailed_error = (
            "g++: internal compiler error: File size limit exceeded signal terminated program as\n"
            "Please submit a full bug report."
        )
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            status="failed",
            detail={
                "status": "failed",
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "source": "manual_validate.cpp",
                    }
                ],
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-main-correct-diag-{uuid.uuid4().hex[:8]}",
                    "task_kind": "main-correct",
                    "source_path": "solutions/std.cpp",
                    "logical_run_id": "r-main-correct-diag",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_FAILED,
                    "verdict": "CE",
                    "runtime_sec": None,
                    "cpu_sec": None,
                    "wall_sec": None,
                    "memory_kb": None,
                    "compile_log": detailed_error,
                    "diagnostics_json": json.dumps(
                        [{"level": "error", "message": detailed_error}],
                        separators=(",", ":"),
                    ),
                    "error_text": detailed_error,
                    "feedback_text": "",
                    "output_ref": "",
                }
            ],
            edges=[],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("<h2>Diagnostics</h2>", html)
        self.assertIn("File size limit exceeded", html)
        self.assertIn("Please submit a full bug report.", html)
        self.assertNotIn(">CE</pre>", html)

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
            signature="",
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
        page = run_details_page(_request("/problems/alice/sample/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("No per-test details yet.", html)
        self.assertIn("001.in", html)
        self.assertIn('vcode">FL</span>', html)

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("accepted solution failed on 001.in", detail_html)

    def test_run_details_shows_sanity_diagnostics(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-sanity-popup-{uuid.uuid4().hex[:8]}"
        run_id = f"r-sanity-popup-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-sanity-popup"),
            kind=Kind.ALL,
            status="failed",
            created_at="2026-04-17T00:00:00Z",
            finished_at="2026-04-17T00:00:02Z",
            runs=[
                {
                    "id": run_id,
                    "status": "failed",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "tests": [{"test": "003.in", "verdict": "WA", "feedback_files": []}],
                        "tests_total": 1,
                    },
                }
            ],
            summary_extra={
                "sanity_status": "failed",
                "sanity_checked_count": 1,
                "sanity_checks": ["empty_output_stability", "unicode_output_stability", "custom_sample_output"],
                "validation_status": "failed",
                "validated_count": 1,
                "failed_step": "sanity",
                "failed_check": "custom_sample_output",
                "failed_test": "003.in",
                "error": "validator reported mismatch",
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-sanity-popup-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "003.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "WA",
                    "runtime_sec": 0.01,
                    "cpu_sec": 0.01,
                    "wall_sec": 0.01,
                    "memory_kb": 1,
                }
            ],
            edges=[],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Sanity", html)
        self.assertIn("Sanity check", html)
        self.assertIn("verification-sanity-detail-popup", html)
        self.assertIn(">details</a>", html)
        self.assertNotIn("Ran 1 of 1 sanity checks.", html)
        self.assertIn("Custom sample output", html)
        self.assertIn("validator reported mismatch", html)

    def test_run_details_sanity_warning_is_visible_without_failed_verification(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-sanity-warning-{uuid.uuid4().hex[:8]}"
        run_id = f"run-warning-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-sanity-warning"),
            kind=Kind.ALL,
            status="ok",
            created_at="2026-04-17T00:00:00Z",
            finished_at="2026-04-17T00:00:02Z",
            runs=[
                {
                    "id": run_id,
                    "status": "ok",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "tests": [{"test": "001.in", "verdict": "OK", "feedback_files": []}],
                        "tests_total": 1,
                    },
                }
            ],
            summary_extra={
                "sanity_status": "warning",
                "sanity_checked_count": 3,
                "sanity_checks": ["empty_output_stability", "unicode_output_stability", "boundary_coverage"],
                "validation_status": "warning",
                "validated_count": 3,
                "failed_step": "sanity",
                "failed_check": "boundary_coverage",
                "failed_test": "",
                "error": "boundary coverage missing: n max=3",
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-sanity-warning-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "runtime_sec": 0.01,
                    "cpu_sec": 0.01,
                    "wall_sec": 0.01,
                    "memory_kb": 1,
                }
            ],
            edges=[],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Sanity check", html)
        self.assertIn("verification-sanity-detail-popup", html)
        self.assertNotIn("Ran 1 of 1 sanity checks.", html)
        self.assertIn("Boundary coverage", html)
        self.assertIn("boundary coverage missing: n max=3", html)

    def test_run_details_sanity_failed_keeps_verification_status_ok(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-sanity-ok-failed-{uuid.uuid4().hex[:8]}"
        run_id = f"run-sanity-ok-failed-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-sanity-ok-failed"),
            kind=Kind.ALL,
            status="ok",
            created_at="2026-04-17T00:00:00Z",
            finished_at="2026-04-17T00:00:02Z",
            runs=[
                {
                    "id": run_id,
                    "status": "ok",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "tests": [{"test": "001.in", "verdict": "OK", "feedback_files": []}],
                        "tests_total": 1,
                    },
                }
            ],
            summary_extra={
                "sanity_status": "failed",
                "sanity_checked_count": 1,
                "sanity_checks": ["empty_output_stability"],
                "validation_status": "failed",
                "validated_count": 1,
                "failed_step": "sanity",
                "failed_check": "empty_output_stability",
                "failed_test": "001.in",
                "error": "empty output probe was accepted",
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-sanity-ok-failed-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "runtime_sec": 0.01,
                    "cpu_sec": 0.01,
                    "wall_sec": 0.01,
                    "memory_kb": 1,
                }
            ],
            edges=[],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("ok (sanity failed)", html)
        self.assertIn("verification-sanity-detail-popup", html)
        self.assertNotIn("Ran 1 of 1 sanity checks.", html)
        self.assertIn("empty output probe was accepted", html)

    def test_run_details_marks_answer_correct_runtime_threshold_times(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-runtime-threshold-{uuid.uuid4().hex[:8]}"
        slow_run_id = f"run-runtime-threshold-{uuid.uuid4().hex[:8]}"
        mixed_run_id = f"run-runtime-mixed-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-runtime-threshold"),
            kind=Kind.ALL,
            status="ok",
            created_at="2026-04-17T00:00:00Z",
            finished_at="2026-04-17T00:00:02Z",
            runs=[],
            summary_extra={
                "mode": "pass-fail",
                "selected_test_names": ["001.in", "002.in", "003.in"],
                "source_paths": ["solutions/slow.cpp", "solutions/mixed.cpp"],
                "sanity_status": "warning",
                "sanity_checked_count": 8,
                "sanity_checks": ["empty_output_stability", "unicode_output_stability", "summary_runtime_threshold", "boundary_coverage"],
                "validation_status": "warning",
                "validated_count": 8,
                "failed_step": "sanity",
                "failed_check": "summary_runtime_threshold",
                "failed_test": "",
                "error": "solutions/slow.cpp: accepted solution is close to the time limit.",
                "sanity_check_results": [
                    {"name": "empty_output_stability", "status": "passed", "checked_count": 1, "messages": []},
                    {"name": "unicode_output_stability", "status": "passed", "checked_count": 1, "messages": []},
                    {
                        "name": "summary_runtime_threshold",
                        "status": "warning",
                        "checked_count": 6,
                        "messages": [
                            {
                                "severity": "warning",
                                "test_name": "",
                                "message": "solutions/slow.cpp: accepted solution is close to the time limit.",
                            },
                            {
                                "severity": "warning",
                                "test_name": "",
                                "message": "solutions/ac_python.py: correct output in 50% extra time limit.",
                            },
                        ],
                    },
                    {"name": "boundary_coverage", "status": "passed", "checked_count": 1, "messages": []},
                ],
                "run_config_json": '{"time_limit_ms":1000,"memory_limit_mb":1024,"pass_limit":1}',
            },
        )
        VerificationTaskStore(config.db).replace_graph(
            verification_id,
            tasks=[
                {
                    "id": f"vt-runtime-1-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/slow.cpp",
                    "logical_run_id": slow_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "cpu_sec": 0.6,
                    "runtime_sec": 0.6,
                    "wall_sec": 0.6,
                    "memory_kb": 1024,
                    "answer_correct": True,
                },
                {
                    "id": f"vt-runtime-2-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/slow.cpp",
                    "logical_run_id": slow_run_id,
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "cpu_sec": 1.2,
                    "runtime_sec": 1.2,
                    "wall_sec": 1.2,
                    "memory_kb": 1024,
                    "answer_correct": True,
                },
                {
                    "id": f"vt-runtime-3-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/slow.cpp",
                    "logical_run_id": slow_run_id,
                    "test_name": "003.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "cpu_sec": 0.2,
                    "runtime_sec": 0.2,
                    "wall_sec": 0.2,
                    "memory_kb": 1024,
                    "answer_correct": True,
                },
                {
                    "id": f"vt-runtime-4-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/mixed.cpp",
                    "logical_run_id": mixed_run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "OK",
                    "cpu_sec": 0.4,
                    "runtime_sec": 0.4,
                    "wall_sec": 0.4,
                    "memory_kb": 1024,
                    "answer_correct": True,
                },
                {
                    "id": f"vt-runtime-5-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/mixed.cpp",
                    "logical_run_id": mixed_run_id,
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "TL",
                    "cpu_sec": 1.2,
                    "runtime_sec": 1.2,
                    "wall_sec": 1.2,
                    "memory_kb": 1024,
                    "answer_correct": False,
                },
                {
                    "id": f"vt-runtime-6-{uuid.uuid4().hex[:8]}",
                    "task_kind": "solution-run",
                    "source_path": "solutions/mixed.cpp",
                    "logical_run_id": mixed_run_id,
                    "test_name": "003.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStore.TASK_DONE,
                    "verdict": "WA",
                    "cpu_sec": 0.7,
                    "runtime_sec": 0.7,
                    "wall_sec": 0.7,
                    "memory_kb": 1024,
                    "answer_correct": False,
                },
            ],
            edges=[],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Summary runtime threshold", html)
        self.assertIn("solutions/slow.cpp: accepted solution is close to the time limit.", html)
        self.assertIn("solutions/ac_python.py: correct output in 50% extra time limit.", html)
        self.assertNotIn("TL(AC)", html)

    def test_run_list_and_submenu_show_sanity_suffix_without_failed_row(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-sanity-list-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-sanity-list"),
            kind=Kind.ALL,
            status="ok",
            created_at="2026-04-17T00:00:00Z",
            finished_at="2026-04-17T00:00:02Z",
            runs=[],
            summary_extra={
                "sanity_status": "warning",
                "sanity_checked_count": 3,
                "sanity_checks": ["boundary_coverage"],
                "validation_status": "warning",
                "validated_count": 3,
                "failed_step": "sanity",
                "failed_check": "boundary_coverage",
                "error": "boundary coverage missing: n max=3",
            },
        )

        rows = workspace_impl.run_list_rows(
            problem_id,
            workspace_id,
            Path(ctx["workspace"]["path"]),
            limit=10,
            actor_user_id=int(ctx["user"]["id"]),
        )
        row = next(item for item in rows if str(item.get("id")) == verification_id)
        self.assertEqual(str(row.get("status")), "ok")
        self.assertEqual(str(row.get("status_display")), "ok (has warning)")
        self.assertEqual(str(row.get("status_tone")), "warn")
        self.assertEqual(str(row.get("fail_reason")), "boundary coverage missing: n max=3")
        self.assertFalse(bool(row.get("is_failed")))

        page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("ok (has warning)", html)
        self.assertIn("boundary coverage missing: n max=3", html)

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
            INSERT INTO previews(id,problem_id,workspace_id,verification_id,source_commit,source_ref,status,summary_json,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                None,
                "",
                "main",
                "failed",
                json.dumps({}),
                "2026-02-23T00:01:00Z",
                "2026-02-23T00:01:01Z",
            ],
        )
        write_preview_summary(preview_id, {"statement_signature": statement_sig})
        preview_resp = preview_page(_request("/problems/alice/sample/preview", f"preview_id={preview_id}"), "alice/sample", "alice")
        preview_html = preview_resp.body.decode("utf-8", errors="replace")
        self.assertNotIn("src=statement", preview_html)
        self.assertNotIn(f"sid={preview_id}", preview_html)
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
            artifact_path=str(config.fs_manager.prepare_verification_root(f"ver-{run_id}").resolve()),
            created_at="2026-02-23T00:02:00Z",
            finished_at="2026-02-23T00:02:01Z",
        )
        run_resp = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        run_html = run_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/problems/alice/sample/run/details?verification_id=ver-{run_id}", run_html)
    def test_run_verification_details_prefers_verification_record_over_audit(self) -> None:
        from app.impl.workspace.run_view_lifecycle_card import load_verification_detail_summary

        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-details")
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="deadbeef",
            status="ok",
            summary=json.dumps({}),
            artifact_path=str(Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / "alice" / "sample" / build_id),
            created_at="2026-03-12T00:00:00Z",
            finished_at="2026-03-12T00:00:01Z",
        )
        verification_id = f"inv-ver-details-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
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
        details_row = load_verification_detail_summary(problem_id, verification_id)
        self.assertEqual(str(details_row.get("created_at") or ""), "2026-03-12T00:00:02Z")
        details = details_row.get("details")
        self.assertIsInstance(details, dict)
        self.assertEqual(str(details.get("verification_id") or ""), verification_id)
        self.assertEqual(str(details.get("status") or ""), "running")
        self.assertFalse(bool(details.get("task_graph")))

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
            kind=Kind.ALL,
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

        page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(verification_id, html)
        self.assertIn("failed", html)

    def test_run_list_reason_uses_short_display_and_full_title(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-list-reason-{uuid.uuid4().hex[:8]}"
        long_reason = "first line\n" + ("very long reason " * 40).strip()
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-list-reason"),
            kind=Kind.ALL,
            status="failed",
            created_at="2026-03-13T00:00:00Z",
            finished_at="2026-03-13T00:00:01Z",
            runs=[],
            summary_extra={"status": "failed"},
        )
        db_execute("UPDATE verifications SET fail_reason=? WHERE id=?", [long_reason, verification_id])

        rows = workspace_impl.run_list_rows(
            problem_id,
            workspace_id,
            Path(ctx["workspace"]["path"]),
            limit=10,
            actor_user_id=int(ctx["user"]["id"]),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("fail_reason") or ""), long_reason)
        self.assertTrue(str(row.get("fail_reason_display") or ""))
        self.assertNotIn("\n", str(row.get("fail_reason_display") or ""))
        self.assertTrue(str(row.get("fail_reason_title") or ""))

    def test_run_details_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        from app.impl.workspace.run_view_lifecycle_card import load_verification_detail_summary

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
            kind=Kind.ALL,
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

        snapshot = load_verification_detail_summary(problem_id, verification_id)
        details = snapshot.get("details")
        self.assertIsInstance(details, dict)
        self.assertEqual(str(details.get("status") or ""), "failed")

    def test_sidebar_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-sidebar-status")
        verification_id = f"ver-sidebar-stale-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.ALL,
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
            },
        )

        page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('class="submenu-status-heading">Verification</strong>', html)
        self.assertIn(">failed</a>", html)
