from __future__ import annotations

from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_execute,
    db_fetch_one,
    verification_programs_for_tasks,
    write_preview_summary,
)
from tests.execution_result_helpers import execution_result

import asyncio
from html import unescape
import io
import os
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException

from app.config import CONFIG_REGISTRY
from app.service.problem.test_spec import dumps_default_tests_spec
from app.service.statement.render import statement_title_for_language
from app.service.statement.signature import statement_sources_signature
from app.service.verification.workspace_fingerprint import verification_sources_signature
from tests.common import E2ETestBase, override_config_values
from tests.identity_helpers import canonical_test_verification_id
from tests.ui_support import (
    Path,
    UIHelpersMixin,
    _flash_messages_from_response,
    _request,
    _wait_for_row,
    tests_page,
    tests_spec_add_gen,
    tests_spec_edit,
    tests_spec_add_manual,
    tests_spec_add_manual_upload,
    tests_spec_delete,
    tests_spec_gen_script_save,
    tests_spec_payload_download,
    tests_spec_payload_upload,
    tests_spec_reindex,
    config,
    general_page,
    json,
    export_page,
    revision_commit,
    preview_page,
    run_details_page,
    run_details_test_fragment,
    run_execute,
    run_cancel,
    run_rejudge,
    artifact_file,
    run_new_page,
    run_page,
    uuid,
    verification_start,
    workspace_service,
)

import app.impl.workspace.context_job as workspace_context_job
from app.impl.workspace.verification_dag import run_workspace_verification_dag
import app.service.problem.readiness as problem_readiness_module
import app.service.verification.workspace_fingerprint as workspace_fingerprint_module
from app.service.problem.readiness import WorkspaceReadinessSubject
from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    CAPTURE_METADATA_ONLY,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
    normalize_execution_result,
)
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.types import Kind

TEXTAREA_MAX_BYTES = int(CONFIG_REGISTRY.defaults()["TEXTAREA_MAX_BYTES"])
STATEMENT_SAMPLE_MAX_BYTES = int(
    CONFIG_REGISTRY.defaults()["STATEMENT_SAMPLE_MAX_BYTES"]
)


class TestUIRun(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = False
    seed_default_workspace = True

    class _FakeUpload:
        def __init__(self, data: bytes):
            self._buf = io.BytesIO(data)

        async def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        async def close(self) -> None:
            return None

    def _problem_readiness(
        self,
        *,
        problem_id: int,
        workspace_id: int,
        workspace_path: Path,
        dirty: bool = True,
    ) -> dict[str, object]:
        workspace_row = config.workspace_service.workspace_rows(
            [problem_id],
            config.workspace_service.known_user_id("alice"),
        )[problem_id]
        subject: WorkspaceReadinessSubject = {
            "problem_id": problem_id,
            "workspace_id": workspace_id,
            "workspace_path": workspace_path,
            "head_commit": workspace_row["head_commit"],
            "dirty": dirty,
            "local_revision": workspace_row["revision_local"],
            "upstream_revision": workspace_row["revision_upstream"],
            "needs_update": False,
        }
        package = {
            "problem_id": problem_id,
            "published_commit": workspace_row["head_commit"],
            "published_revision_number": workspace_row["revision_upstream"],
            "materialized_revision_number": None,
            "materialization_id": "",
            "status": "none",
            "missing_reason": "Package not built",
        }
        with patch.object(
            config.problem_package_service,
            "published_readiness",
            return_value=package,
        ):
            return dict(
                config.problem_readiness_service.readiness(
                    subject,
                    explain_verification=True,
                )
            )

    @staticmethod
    def _verification_id_for_run(run_id: str) -> str:
        return canonical_test_verification_id(f"run:{run_id}")

    def _admit_verification_fixture(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int | None,
        signature: str = "",
        source_commit: str = "",
        kind: str = Kind.ALL,
        detail: dict[str, object] | None = None,
    ) -> None:
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            source_commit=source_commit,
            kind=str(kind),
        )
        self.assertEqual(admission.outcome, "admitted")
        if detail is not None:
            pending = getattr(self, "_pending_verification_fixture_details", {})
            pending[verification_id] = dict(detail)
            self._pending_verification_fixture_details = pending

    def _activate_verification_fixture(
        self,
        verification_id: str,
        *,
        detail: dict[str, object] | None = None,
        tasks: list[PlannedTask],
        completions: list[TaskCompletion] | None = None,
        queued: list[tuple[str, str, str]] | None = None,
        leased: list[tuple[str, str, str]] | None = None,
    ) -> None:
        pending = getattr(self, "_pending_verification_fixture_details", {})
        activation_detail = (
            dict(pending.pop(verification_id, {}))
            if detail is None
            else dict(detail)
        )
        canonical_tasks = list(tasks)
        canonical_completions = list(completions or [])
        if not any(task.program_id == "accepted" for task in canonical_tasks):
            accepted_test_name = canonical_tasks[0].test_name
            accepted_task_id = verification_task_id(
                verification_id,
                "accepted",
                accepted_test_name,
            )
            canonical_tasks.insert(
                0,
                PlannedTask(
                    task_id=accepted_task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name=accepted_test_name,
                    expected_behavior="accepted",
                ),
            )
            canonical_completions.insert(
                0,
                TaskCompletion(
                    task_id=accepted_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result("OK"),
                ),
            )
        activation = activate_test_verification(
            verification_id,
            detail=activation_detail,
            programs=verification_programs_for_tasks(canonical_tasks),
            tasks=canonical_tasks,
        )
        self.assertEqual(activation.outcome, "activated")
        for task_id, run_id, judgehost_task_id in [*(queued or []), *(leased or [])]:
            self.assertTrue(
                config.verification_task_store.bind_and_expose_judgehost_runtime(
                    task_id,
                    run_id=run_id,
                    judgehost_task_id=judgehost_task_id,
                    expose=lambda: None,
                )
            )
        for task_id, _run_id, _judgehost_task_id in leased or []:
            config.verification_task_store.set_task_leased(task_id)
        if canonical_completions:
            config.verification_task_store.commit_task_completions(
                canonical_completions
            )

    @staticmethod
    def _fixture_result(
        summary: dict[str, object],
        *,
        status: str,
    ) -> ExecutionResult:
        tests_obj = summary.get("tests")
        tests = tests_obj if isinstance(tests_obj, list) else []
        test = tests[0] if tests and isinstance(tests[0], dict) else {}
        verdict = str(test.get("verdict") or summary.get("verdict") or "")
        if not verdict and status == "ok":
            verdict = "OK"
        if not verdict and status == "failed":
            verdict = "FL"
        return execution_result(
            verdict,
            runtime_sec=float(test.get("runtime_sec") or 0.0),
            cpu_sec=float(test.get("cpu_sec") or 0.0),
            wall_sec=float(test.get("wall_sec") or 0.0),
            memory_kb=int(test.get("memory_kb") or 0),
            error=str(test.get("error") or summary.get("error") or ""),
            feedback=str(test.get("feedback") or ""),
            output_ref=str(test.get("output_ref") or ""),
        )

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
        activate_tasks: bool = True,
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
        if existing_row is not None:
            db_execute(
                "UPDATE verifications SET created_at=? WHERE id=?",
                [final_created_at, verification_id],
            )
            return
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            source_commit="",
            kind=kind_token,
            detail=metadata,
        )
        if activate_tasks:
            planned: list[PlannedTask] = []
            completions: list[TaskCompletion] = []
            leased: list[tuple[str, str, str]] = []
            run_items = runs or [
                {
                    "id": f"fixture-{verification_id}",
                    "status": status,
                    "source_label": (source_paths[0] if source_paths else "solutions/fixture.cpp"),
                    "expected_behavior": "accepted",
                    "summary": {},
                }
            ]
            for slot, item in enumerate(run_items):
                run_id = str(item.get("id") or f"fixture-{verification_id}-{slot}")
                summary_obj = dict(item.get("summary") or {})
                tests_obj = summary_obj.get("tests")
                tests = tests_obj if isinstance(tests_obj, list) else []
                first_test = tests[0] if tests and isinstance(tests[0], dict) else {}
                test_name = str(first_test.get("test") or "001.in")
                program_id = f"solution-{slot}"
                task_id = verification_task_id(
                    verification_id,
                    program_id,
                    test_name,
                )
                source_path = str(
                    summary_obj.get("source")
                    or item.get("source_label")
                    or f"solutions/fixture-{slot}.cpp"
                )
                planned.append(
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="solution-run",
                        source_path=source_path,
                        program_id=program_id,
                        test_name=test_name,
                        expected_behavior=str(item.get("expected_behavior") or "unknown"),
                    )
                )
                item_status = str(item.get("status") or status).lower()
                if item_status == "running":
                    leased.append((task_id, run_id, f"jt-fixture-{slot}-{verification_id}"))
                    continue
                fail_reason = ""
                if status == "failed" and not completions:
                    fail_reason = str(metadata.get("error") or "verification fixture failed")
                completions.append(
                    TaskCompletion(
                        task_id=task_id,
                        status=VerificationTaskStore.TASK_DONE,
                        run_id=run_id,
                        judgehost_task_id="",
                        result=self._fixture_result(summary_obj, status=item_status),
                        fail_reason=fail_reason,
                    )
                )
            self._activate_verification_fixture(
                verification_id,
                detail=metadata,
                tasks=planned,
                completions=completions,
                leased=leased,
            )
            if status == "failed":
                record = config.verification_service.verification_record(verification_id)
                if record is not None and str(record["status"]) == "running":
                    config.verification_service.fail_verification(
                        verification_id,
                        reason=str(metadata.get("error") or "verification fixture failed"),
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
        workspace_id: int | None,
        kind: str = Kind.ALL,
        signature: str = "",
        status: str = "ok",
        source_commit: str = "",
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
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=str(signature or "").strip(),
            source_commit=str(source_commit or "").strip(),
            kind=str(kind or Kind.ALL).strip() or Kind.ALL.value,
            detail=summary_obj,
        )
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        task = PlannedTask(
            task_id=task_id,
            predecessor_task_id=None,
            task_kind="solution-run",
            source_path="solutions/fixture.cpp",
            program_id="solution-0",
            test_name="001.in",
            expected_behavior="accepted",
        )
        completions: list[TaskCompletion] = []
        leased: list[tuple[str, str, str]] = []
        if status == "ok":
            completions.append(
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result("OK"),
                )
            )
        elif status == "running":
            leased.append((task_id, f"fixture-{verification_id}", f"jt-fixture-{verification_id}"))
        self._activate_verification_fixture(
            verification_id,
            detail=summary_obj,
            tasks=[task],
            completions=completions,
            leased=leased,
        )
        if status == "failed":
            failure = config.verification_service.fail_verification(
                verification_id,
                reason=str(summary_obj.get("error") or "verification fixture failed"),
            )
            self.assertEqual(failure.outcome, "transitioned")
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
            or self._verification_id_for_run(run_id)
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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
        self.assertIn("/problems/alice/sample/tests", add_manual_loc)
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

    def test_tests_spec_edit_can_clear_sample_output_validate(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        manual_dir = ws / "tests" / "manual"
        generator_dir = ws / "tests" / "generator"
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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
            sample="1",
            manual_input="1\n",
            sample_output="42\n",
            sample_output_validate=["0", "1"],
        )
        self.assertEqual(add_manual.status_code, 303)

        edit_spec = tests_spec_edit(
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

        edit_spec_checked = tests_spec_edit(
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

        page = tests_page(_request("/problems/alice/sample/tests"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('type="hidden" name="sample_output_validate" value="0"', html)

    def test_tests_spec_gen_script_save_adds_and_removes_gen_entries(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        manual_dir = ws / "tests" / "manual"
        generator_dir = ws / "tests" / "generator"
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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
            gen_script_text="gen 10 1\r\ngen 30 3\r\n",
        )
        self.assertEqual(updated.status_code, 303)
        self.assertTrue(str(updated.headers.get("location", "")).endswith("/problems/alice/sample/tests"))

        configured_page = tests_page(
            _request("/problems/alice/sample/tests"), "alice/sample", "alice"
        )
        configured_html = configured_page.body.decode("utf-8", errors="replace")
        self.assertIn("Generation script", configured_html)
        self.assertIn("configured &middot; 2 commands", configured_html)
        self.assertIn("Edit generation script", configured_html)
        self.assertNotIn('id="tests-gen-script-text"', configured_html)

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual([(row.get("id"), row.get("kind")) for row in tests], [("001", "manual"), ("002", "gen"), ("003", "gen")])
        self.assertEqual((generator_dir / "002.in").read_text(encoding="utf-8"), "gen 10 1")
        self.assertEqual((generator_dir / "003.in").read_text(encoding="utf-8"), "gen 30 3")
        self.assertNotIn("\r", (generator_dir / "002.in").read_text(encoding="utf-8"))
        self.assertNotIn("\r", (generator_dir / "003.in").read_text(encoding="utf-8"))

        cleared = tests_spec_gen_script_save(problem="alice/sample", user="alice", gen_script_text="")
        self.assertEqual(cleared.status_code, 303)
        payload_after = json.loads(spec_path.read_text(encoding="utf-8"))
        tests_after = payload_after.get("tests") or []
        self.assertEqual([(row.get("id"), row.get("kind")) for row in tests_after], [("001", "manual")])
        self.assertFalse((generator_dir / "002.in").exists())
        self.assertFalse((generator_dir / "003.in").exists())

    def test_tests_gen_script_save_error_returns_to_editor(self) -> None:
        with patch(
            "app.impl.tests_spec.routes.parse_gen_script_lines",
            side_effect=ValueError("invalid generator command"),
        ):
            response = tests_spec_gen_script_save(
                problem="alice/sample",
                user="alice",
                gen_script_text="bad command",
            )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            str(response.headers.get("location", "")).endswith(
                "/problems/alice/sample/tests?edit=gen-script"
            )
        )
        self.assertTrue(
            any(
                "invalid generator command" in message
                for message in _flash_messages_from_response(response)
            )
        )

    def test_tests_spec_large_manual_disables_inline_editor_and_shows_payload_actions(self) -> None:
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace = Path(str(ws_ctx["workspace"]["path"]))
        spec_path = workspace / "tests" / "spec.json"
        manual_dir = workspace / "tests" / "manual"
        generator_dir = workspace / "tests" / "generator"
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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

        page = tests_page(_request("/problems/alice/sample/tests"), "alice/sample", "alice")
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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

        upload_payload = self._FakeUpload(b"7 8 9  \r\n10 11\t \r\n")
        uploaded = asyncio.run(
            tests_spec_payload_upload(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=upload_payload,
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertIn("/problems/alice/sample/tests", uploaded.headers.get("location", ""))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "7 8 9\n10 11\n")

        downloaded = tests_spec_payload_download(problem="alice/sample", user="alice", index="1")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("001.in", str(downloaded.headers.get("content-disposition", "")))

    def test_tests_spec_manual_payload_upload_accepts_payloads_larger_than_textarea_limit(self) -> None:
        oversized = (b"8" * (TEXTAREA_MAX_BYTES + 32)) + b"\r\n"
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace = Path(str(ws_ctx["workspace"]["path"]))
        spec_path = workspace / "tests" / "spec.json"
        manual_dir = workspace / "tests" / "manual"
        generator_dir = workspace / "tests" / "generator"
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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

        uploaded = asyncio.run(
            tests_spec_payload_upload(
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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

        uploaded = asyncio.run(
            tests_spec_payload_upload(
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
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

        override_config_values(self, config.config_values, UPLOAD_MAX_BYTES=1024)
        uploaded = asyncio.run(
            tests_spec_payload_upload(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=self._FakeUpload(b"x" * 1025),
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        upload = self._FakeUpload(b"11 22  \r\n33 44\t \r\n")
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        created = asyncio.run(
            tests_spec_add_manual_upload(
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        created = asyncio.run(
            tests_spec_add_manual_upload(
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
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        override_config_values(self, config.config_values, UPLOAD_MAX_BYTES=1024)
        created = asyncio.run(
            tests_spec_add_manual_upload(
                problem="alice/sample",
                user="alice",
                test_id="",
                sample="0",
                manual_upload=self._FakeUpload(b"x" * 1025),
            )
        )
        self.assertEqual(created.status_code, 303)
        self.assertIn("uploaded payload is too large.", _flash_messages_from_response(created))
        self.assertFalse((manual_dir / "001.in").exists())

    def test_run_execute_without_tests_triggers_implicit_tests_generation(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
        )
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        db_execute("DELETE FROM verifications WHERE workspace_id=?", [workspace_id])
        def _fake_start_verification_job(*args, **kwargs) -> bool:
            verification_id = str(kwargs["verification_id"])
            self._admit_verification_fixture(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                signature="deadbeef",
                kind=Kind.ALL.value,
            )
            task_id = verification_task_id(
                verification_id,
                "solution-0",
                "001.in",
            )
            self._activate_verification_fixture(
                verification_id,
                detail=dict(kwargs.get("initial_summary") or {}),
                tasks=[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="solution-run",
                        source_path="solutions/accepted.cpp",
                        program_id="solution-0",
                        test_name="001.in",
                        expected_behavior="accepted",
                    )
                ],
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

    def test_run_execute_records_problem_mode_from_general_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
        )
        self._update_problem_config(ws, mode="interactive", pass_limit=2)

        with patch(
            "app.impl.run_export.run.start_verification_job",
            return_value=True,
        ):
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
        audit_row = db_fetch_one(
            """
            SELECT details_json
            FROM audit_log
            WHERE action='run.execute' AND details_json LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [f"%{verification_id}%"],
        )
        self.assertIsNotNone(audit_row)
        metadata = json.loads(str(audit_row["details_json"]))
        self.assertEqual(str(metadata.get("mode") or ""), "interactive")

    def test_run_execute_records_verification_audit_before_queue_start(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
            ("wa.cpp", "wrong_answer"),
        )
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        observed = {"checked": False}

        def _fake_start_verification_job(*args, **kwargs) -> bool:
            verification_id = str(kwargs.get("verification_id") or "")
            targets = list(kwargs.get("targets") or [])
            solution_program_ids = [
                str(item.get("program_id") or "")
                for item in targets
                if str(item.get("program_id") or "")
            ]
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
            self.assertEqual(
                [
                    str(item or "")
                    for item in (
                        details.get("solution_program_ids") or []
                    )
                ],
                solution_program_ids,
            )
            self.assertEqual(
                int(details.get("solution_program_count") or 0),
                len(solution_program_ids),
            )
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
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
        )
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
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
        )

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
        for path in (accepted_path, Path(f"{accepted_path}.desc")):
            path.unlink(missing_ok=True)
        self._write_solution_fixture(ws, "foo.cpp", "unknown")
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

    def test_admitted_verification_fails_when_layout_preparation_fails(self) -> None:
        problem = f"alice/layout-failure-{uuid.uuid4().hex[:8]}"
        user = "alice"
        self._prepare_verification_workspace(problem)
        context = workspace_service.workspace_context(
            problem,
            user,
            include_recent=False,
        )
        problem_id = int(context["problem"]["id"])
        workspace_id = int(context["workspace"]["id"])
        verification_id = canonical_test_verification_id(
            f"ver-layout-failure-{uuid.uuid4().hex[:8]}"
        )
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
        )

        with patch.object(
            config.fs_manager,
            "prepare_verification_layout",
            side_effect=RuntimeError("verification layout unavailable"),
        ):
            run_workspace_verification_dag(
                problem,
                user,
                actor_user_id=int(context["user"]["id"]),
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_head="",
                workspace_dirty=True,
                targets=[],
                verification_id=verification_id,
            )

        verification = db_fetch_one(
            "SELECT status,fail_reason,finished_at FROM verifications WHERE id=?",
            [verification_id],
        )
        open_tasks = db_fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM verification_tasks
            WHERE verification_id=? AND final_status=''
            """,
            [verification_id],
        )
        self.assertIsNotNone(verification)
        self.assertIsNotNone(open_tasks)
        self.assertEqual(str(verification["status"]), "failed")
        self.assertEqual(
            str(verification["fail_reason"]),
            "verification layout unavailable",
        )
        self.assertTrue(str(verification["finished_at"] or ""))
        self.assertEqual(int(open_tasks["count"]), 0)

    def test_verification_sidebar_marks_stale_when_gen_chk_sol_tests_change(self) -> None:
        problem = f"alice/verify-stale-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        signature = verification_sources_signature(ws)

        self._insert_verification_row(
            verification_id=canonical_test_verification_id(f"ver-stale-{uuid.uuid4().hex[:8]}"),
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
        self.assertIn("ok (stale)", html)
        self.assertIn("Inputs changed since this verification", html)

    def test_verification_sidebar_marks_stale_when_general_info_changes(self) -> None:
        problem = f"alice/verify-stale-general-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        signature = verification_sources_signature(ws)

        self._insert_verification_row(
            verification_id=canonical_test_verification_id(f"ver-stale-general-{uuid.uuid4().hex[:8]}"),
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verify-stale-general"),
            kind=Kind.ALL,
            status="failed",
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
            runs=[],
            summary_extra={
                "status": "failed",
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
        self.assertIn("failed (stale)", html)
        self.assertIn("Inputs changed since this verification", html)

    def test_verification_sidebar_prefers_current_workspace_signature_over_export_verification(self) -> None:
        problem = f"alice/verify-export-not-stale-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        current_signature = verification_sources_signature(ws)
        workspace_verification_id = canonical_test_verification_id(f"ver-workspace-{uuid.uuid4().hex[:8]}")
        export_verification_id = canonical_test_verification_id(f"ver-export-{uuid.uuid4().hex[:8]}")

        self._insert_stage_verification(
            verification_id=workspace_verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=current_signature,
            status="ok",
            created_at="2026-02-23T00:00:00Z",
        )
        self._insert_stage_verification(
            verification_id=export_verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=f"snapshot-{uuid.uuid4().hex}",
            status="ok",
            source_commit="0123456789abcdef",
            created_at="2026-02-23T00:00:01Z",
        )

        readiness = self._problem_readiness(
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_path=ws,
        )
        status = readiness["verification"]
        self.assertEqual(status["result"], "ok")
        self.assertEqual(status["verification_id"], workspace_verification_id)
        self.assertFalse(status["stale"])

    def test_verification_sidebar_matches_clean_workspace_manifest_signature(self) -> None:
        problem = f"alice/verify-clean-manifest-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-clean-manifest-{uuid.uuid4().hex[:8]}")
        signature = workspace_fingerprint_module.verification_sources_signature(ws)
        self._insert_stage_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            status="ok",
        )
        readiness = self._problem_readiness(
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_path=ws,
            dirty=False,
        )
        status = readiness["verification"]
        self.assertEqual(status["verification_id"], verification_id)
        self.assertFalse(status["stale"])

    def test_clean_workspace_matches_canonical_workspace_source_commit(self) -> None:
        problem = f"alice/verify-clean-source-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        commit_resp = revision_commit(
            problem=problem,
            user="alice",
            message=f"verify-clean-source-{uuid.uuid4().hex[:6]}",
        )
        self.assertEqual(commit_resp.status_code, 303)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        user_id = int(ctx["user"]["id"])
        status = workspace_service.refresh_workspace_status_with_ids(
            ws,
            problem_id,
            user_id,
        )
        head_commit = str(status["head_commit"])
        self.assertTrue(head_commit)
        verification_id = canonical_test_verification_id(
            f"ver-clean-source-{uuid.uuid4().hex[:8]}"
        )
        self._insert_stage_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=f"old-signature-{uuid.uuid4().hex}",
            source_commit=f"workspace:{head_commit}",
            status="ok",
        )

        with patch.object(
            problem_readiness_module,
            "verification_sources_signature",
            side_effect=AssertionError("clean source identity should avoid hashing"),
        ) as full_hash:
            status = self._problem_readiness(
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_path=ws,
                dirty=False,
            )["verification"]

        full_hash.assert_not_called()
        self.assertEqual(status["verification_id"], verification_id)
        self.assertFalse(status["stale"])

    def test_verification_sidebar_fingerprint_cache_skips_full_hash(self) -> None:
        problem = f"alice/verify-fingerprint-cache-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "tests" / "manual" / "large.bin").write_bytes(b"x" * 4096)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-cache-{uuid.uuid4().hex[:8]}")
        fingerprint = workspace_fingerprint_module.verification_sources_fingerprint(ws)

        self._insert_stage_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=f"sig-{uuid.uuid4().hex}",
            status="ok",
        )
        workspace_fingerprint_module.remember_verification_fingerprint(
            problem_id,
            workspace_id,
            fingerprint,
            verification_id,
        )

        with patch.object(
            problem_readiness_module,
            "verification_sources_signature",
            side_effect=AssertionError("full hash should be skipped"),
        ) as full_hash:
            readiness = self._problem_readiness(
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_path=ws,
            )
            status = readiness["verification"]

        full_hash.assert_not_called()
        self.assertEqual(status["verification_id"], verification_id)
        self.assertFalse(status["stale"])

    def test_verification_sidebar_full_hash_match_populates_fingerprint_cache(self) -> None:
        problem = f"alice/verify-fingerprint-fill-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        signature = verification_sources_signature(ws)
        verification_id = canonical_test_verification_id(f"ver-fill-{uuid.uuid4().hex[:8]}")

        self._insert_stage_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            status="ok",
        )

        first = self._problem_readiness(
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_path=ws,
        )["verification"]
        self.assertEqual(first["verification_id"], verification_id)
        self.assertFalse(first["stale"])

        with patch.object(
            problem_readiness_module,
            "verification_sources_signature",
            side_effect=AssertionError("full hash should be cached after first match"),
        ) as full_hash:
            second = self._problem_readiness(
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_path=ws,
            )["verification"]

        full_hash.assert_not_called()
        self.assertEqual(second["verification_id"], verification_id)
        self.assertFalse(second["stale"])

    def test_verification_sidebar_stale_fingerprint_cache_skips_repeated_full_hash(self) -> None:
        problem = f"alice/verify-fingerprint-stale-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-stale-cache-{uuid.uuid4().hex[:8]}")

        self._insert_stage_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=f"old-{uuid.uuid4().hex}",
            status="ok",
        )

        first = self._problem_readiness(
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_path=ws,
        )["verification"]
        self.assertEqual(first["verification_id"], verification_id)
        self.assertTrue(first["stale"])

        with patch.object(
            problem_readiness_module,
            "verification_sources_signature",
            side_effect=AssertionError("stale fingerprint should cache current signature"),
        ) as full_hash:
            second = self._problem_readiness(
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_path=ws,
            )["verification"]

        full_hash.assert_not_called()
        self.assertEqual(second["verification_id"], verification_id)
        self.assertTrue(second["stale"])

    def test_run_page_shows_multi_solution_selector_without_mode_select(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
            ("wa.cpp", "wrong_answer"),
        )

        page = run_new_page(_request("/problems/alice/sample/run/new", "solution_paths=solutions/wa.cpp"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("id=\"solution-paths\"", html)
        self.assertIn("type=\"checkbox\" name=\"solution_paths\"", html)
        self.assertIn("id=\"test-names\"", html)
        self.assertTrue(("type=\"checkbox\" name=\"test_names\"" in html) or ("No tests." in html))
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
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
            ("wa.cpp", "wrong_answer"),
        )
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-rerun-link-{uuid.uuid4().hex[:8]}")
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
            activate_tasks=False,
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
        rerun_ok_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        rerun_wa_task_id = verification_task_id(
            verification_id,
            "solution-1",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=rerun_ok_task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=rerun_wa_task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/wa.cpp",
                    program_id="solution-1",
                    test_name="001.in",
                    expected_behavior="wrong_answer",
                ),
            ],
            completions=[
                TaskCompletion(
                    task_id=rerun_ok_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=run_ok,
                    judgehost_task_id="",
                    result=execution_result("OK"),
                ),
                TaskCompletion(
                    task_id=rerun_wa_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=run_wa,
                    judgehost_task_id="",
                    result=execution_result("WA"),
                ),
            ],
        )
        list_page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(list_page.status_code, 200)
        list_html = list_page.body.decode("utf-8", errors="replace")
        self.assertIn('action="/problems/alice/sample/run/rejudge"', list_html)
        self.assertIn(f'name="verification_id" value="{verification_id}"', list_html)

        details_page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(details_page.status_code, 200)
        detail_html = details_page.body.decode("utf-8", errors="replace")
        self.assertIn('action="/problems/alice/sample/run/rejudge"', detail_html)
        self.assertIn(f'name="verification_id" value="{verification_id}"', detail_html)

        with patch("app.impl.run_export.run.start_verification_job", return_value=True) as start_job:
            response = run_rejudge("alice/sample", "alice", verification_id=verification_id)

        self.assertEqual(response.status_code, 303)
        call_kwargs = start_job.call_args.kwargs
        self.assertTrue(call_kwargs["bypass_case_result_cache"])
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

        add_manual_1 = tests_spec_add_manual(problem=problem, user="alice", test_id="001", manual_input="1\n")
        add_manual_2 = tests_spec_add_manual(problem=problem, user="alice", test_id="002", manual_input="2\n")
        self.assertEqual(add_manual_1.status_code, 303)
        self.assertEqual(add_manual_2.status_code, 303)

        page = run_new_page(_request(f"/problems/{problem}/alice/run/new"), problem, "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('name="test_names" value="001.in" checked', html)
        self.assertIn('name="test_names" value="002.in" checked', html)

    def test_run_list_orders_by_verification_run_time_not_latest_run_time(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        old_verification = canonical_test_verification_id(f"ver-old-{uuid.uuid4().hex[:8]}")
        new_verification = canonical_test_verification_id(f"ver-new-{uuid.uuid4().hex[:8]}")
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

        page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(old_verification, html)
        self.assertIn(new_verification, html)
        self.assertLess(html.index(new_verification), html.index(old_verification))

    def test_published_verification_can_be_rejudged_but_not_cancelled(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
            ("fixture.cpp", "unknown"),
        )
        workspace_service.ensure_user("bob")
        workspace_service.grant_repo_access("alice/sample", "bob", "read")
        bob_ws = Path(
            workspace_service.ensure_workspace(
                "alice/sample",
                "bob",
                refresh_status=False,
            )
        )
        self._configure_solution_fixtures(
            bob_ws,
            ("accepted.cpp", "accepted"),
            ("fixture.cpp", "unknown"),
        )
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        published_id = canonical_test_verification_id(
            f"ver-published-visible-{uuid.uuid4().hex[:8]}"
        )
        running_published_id = canonical_test_verification_id(
            f"ver-published-running-{uuid.uuid4().hex[:8]}"
        )
        self._insert_stage_verification(
            verification_id=published_id,
            problem_id=problem_id,
            workspace_id=None,
            status="ok",
            source_commit=str(ctx["workspace"]["head_commit"] or ""),
            summary={"source_paths": ["solutions/fixture.cpp"]},
        )
        self._insert_stage_verification(
            verification_id=running_published_id,
            problem_id=problem_id,
            workspace_id=None,
            status="running",
            source_commit=str(ctx["workspace"]["head_commit"] or ""),
        )

        list_page = run_page(
            _request("/problems/alice/sample/run"),
            "alice/sample",
            "alice",
        )
        list_html = unescape(
            list_page.body.decode("utf-8", errors="replace")
        )
        self.assertIn(
            "Published verification \u00b7 original is read-only",
            list_html,
        )
        self.assertIn(
            f'name="verification_id" value="{published_id}"',
            list_html,
        )
        self.assertIn(
            "Cancel disabled (You are not the owner of this verification)",
            list_html,
        )

        detail_page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={published_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail_page.status_code, 200)
        self.assertIn(
            "Published verification \u00b7 the original record is read-only",
            unescape(detail_page.body.decode("utf-8", errors="replace")),
        )

        with patch(
            "app.impl.run_export.run.start_verification_job",
            return_value=True,
        ) as start_job:
            rejudge_response = run_rejudge(
                "alice/sample",
                "bob",
                verification_id=published_id,
            )
        self.assertEqual(rejudge_response.status_code, 303)
        start_job.assert_called_once()
        bob_ctx = workspace_service.workspace_context(
            "alice/sample",
            "bob",
            include_recent=False,
        )
        self.assertEqual(
            start_job.call_args.kwargs["workspace_id"],
            int(bob_ctx["workspace"]["id"]),
        )
        cancel_response = run_cancel(
            "alice/sample",
            "alice",
            verification_id=running_published_id,
        )
        self.assertEqual(cancel_response.status_code, 303)
        self.assertTrue(
            any(
                "not the owner of this verification" in message
                for message in _flash_messages_from_response(cancel_response)
            )
        )

    def test_other_workspace_verification_is_hidden_but_readable_and_not_cancellable(self) -> None:
        workspace_service.ensure_user("bob")
        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        workspace_service.ensure_workspace("alice/sample", "bob", refresh_status=False)
        alice_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        bob_ctx = workspace_service.workspace_context("alice/sample", "bob", include_recent=False)
        foreign_id = canonical_test_verification_id(
            f"ver-foreign-hidden-{uuid.uuid4().hex[:8]}"
        )
        self._insert_stage_verification(
            verification_id=foreign_id,
            problem_id=int(alice_ctx["problem"]["id"]),
            workspace_id=int(bob_ctx["workspace"]["id"]),
            status="running",
        )

        list_page = run_page(
            _request("/problems/alice/sample/run"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(list_page.status_code, 200)
        self.assertNotIn(foreign_id, list_page.body.decode("utf-8", errors="replace"))

        detail_page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={foreign_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail_page.status_code, 200)

        cancel_response = run_cancel(
            "alice/sample",
            "alice",
            verification_id=foreign_id,
        )
        self.assertEqual(cancel_response.status_code, 303)
        self.assertEqual(
            config.verification_service.verification_record(foreign_id)["status"],
            "running",
        )

    def test_run_cancel_marks_running_verification_failed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-cancel-{uuid.uuid4().hex[:8]}")
        run_id = f"r-cancel-running-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-cancel-run")
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            activate_tasks=False,
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
        leased_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        pending_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=leased_task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=pending_task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name="002.in",
                    expected_behavior="accepted",
                ),
            ],
            leased=[(leased_task_id, run_id, "jt-cancel-leased")],
        )

        details_before = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(details_before.status_code, 200)

        cancel_resp = run_cancel(problem="alice/sample", user="alice", verification_id=verification_id)
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
            for row in config.verification_task_store.list_rows(verification_id)
        }
        self.assertEqual(str(rows[leased_task_id]["status"] or ""), VerificationTaskStore.TASK_CANCELLED)
        self.assertEqual(str(rows[pending_task_id]["status"] or ""), VerificationTaskStore.TASK_CANCELLED)

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
        verification_id = canonical_test_verification_id(f"inv-cancel-pending-{uuid.uuid4().hex[:8]}")
        build_id = self.random_id("b-cancel-pending")
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            activate_tasks=False,
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-05T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={"task_graph": True, "source_paths": ["solutions/accepted.cpp"]},
        )
        queued_task_ids = [
            verification_task_id(
                verification_id,
                "solution-0",
                f"{index:03d}.in",
            )
            for index in (1, 2)
        ]
        queued_run_ids = [
            f"r-cancel-pending-{index}-{uuid.uuid4().hex[:8]}"
            for index in (1, 2)
        ]
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name=f"{index + 1:03d}.in",
                    expected_behavior="accepted",
                )
                for index, task_id in enumerate(queued_task_ids)
            ],
            queued=[
                (task_id, queued_run_ids[index], f"jt-cancel-pending-{index + 1}")
                for index, task_id in enumerate(queued_task_ids)
            ],
        )
        cancel_resp = run_cancel(problem="alice/sample", user="alice", verification_id=verification_id)
        self.assertEqual(cancel_resp.status_code, 303)
        cancel_messages = _flash_messages_from_response(cancel_resp)
        self.assertTrue(cancel_messages)
        self.assertIn("verification cancelled", cancel_messages[0])
        verification_row = db_fetch_one("SELECT status FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or "").strip().lower(), "failed")
        rows = config.verification_task_store.list_rows(verification_id)
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "solution-0"
            ],
            [VerificationTaskStore.TASK_CANCELLED, VerificationTaskStore.TASK_CANCELLED],
        )
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "accepted"
            ],
            [VerificationTaskStore.TASK_DONE],
        )

    def test_run_cancel_cancels_queued_rows_when_domjudge_has_only_pending_cases(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-cancel-domjudge-pending-{uuid.uuid4().hex[:8]}")
        run_id = f"r-cancel-domjudge-pending-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-cancel-domjudge-pending")
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            activate_tasks=False,
            kind=Kind.ALL,
            status="running",
            created_at="2026-03-05T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={"task_graph": True, "source_paths": ["solutions/accepted.cpp"]},
        )
        queued_task_ids = [
            verification_task_id(
                verification_id,
                "solution-0",
                f"{index:03d}.in",
            )
            for index in (1, 2)
        ]
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name=f"{index + 1:03d}.in",
                    expected_behavior="accepted",
                )
                for index, task_id in enumerate(queued_task_ids)
            ],
            queued=[
                (task_id, run_id, "jt-cancel-domjudge-pending")
                for task_id in queued_task_ids
            ],
        )
        cancel_resp = run_cancel(
            problem="alice/sample",
            user="alice",
            verification_id=verification_id,
        )
        self.assertEqual(cancel_resp.status_code, 303)
        rows = config.verification_task_store.list_rows(verification_id)
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "solution-0"
            ],
            [VerificationTaskStore.TASK_CANCELLED, VerificationTaskStore.TASK_CANCELLED],
        )
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "accepted"
            ],
            [VerificationTaskStore.TASK_DONE],
        )


    def test_run_list_treats_failed_verification_as_terminal_even_with_queued_runs(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-list-failed-{uuid.uuid4().hex[:8]}")
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
        self.assertIn(
            f"/problems/alice/sample/run/details?verification_id={verification_id}",
            html,
        )
        self.assertNotIn(
            f'action="/problems/alice/sample/run/cancel/{verification_id}"',
            html,
        )

    def test_run_details_shows_task_status_for_verification(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-verif-task-status-{uuid.uuid4().hex[:8]}")
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
            activate_tasks=False,
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
        done_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "003.in",
        )
        generating_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        main_task_id = verification_task_id(
            verification_id,
            "accepted",
            "002.in",
        )
        pending_task_ids = [
            verification_task_id(
                verification_id,
                "solution-0",
                f"{index:03d}.in",
            )
            for index in range(4, 9)
        ]
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=done_task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="solutions/accepted.cpp",
                    program_id="generator-0",
                    test_name="003.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=generating_task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="solutions/accepted.cpp",
                    program_id="generator-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=main_task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="002.in",
                    expected_behavior="accepted",
                ),
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="solution-run",
                        source_path="solutions/accepted.cpp",
                        program_id="solution-0",
                        test_name=f"{index:03d}.in",
                        expected_behavior="accepted",
                    )
                    for index, task_id in zip(range(4, 9), pending_task_ids)
                ],
            ],
            completions=[
                TaskCompletion(
                    task_id=done_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result("OK"),
                )
            ],
            leased=[
                (generating_task_id, "generate-001", "jt-generate-001"),
                (main_task_id, run_id, "jt-main-002"),
            ],
        )
        page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={verification_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        for evidence in ("accepted.cpp", "001.in", "002.in", "003.in", "008.in"):
            self.assertIn(evidence, html)

    def test_run_details_running_issue_uses_info_note_and_status_moves_into_task_status(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-verif-running-issue-{uuid.uuid4().hex[:8]}")
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
        verification_id = canonical_test_verification_id(f"inv-verif-task-graph-{uuid.uuid4().hex[:8]}")
        main_run_id = f"r-main-{uuid.uuid4().hex[:8]}"
        solution_run_id = f"r-solution-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-graph"),
            activate_tasks=False,
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
                "signature": verification_sources_signature(ws),
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
        generate_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        main_task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        solution_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=generate_task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="solutions/accepted.cpp",
                    program_id="generator-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=main_task_id,
                    predecessor_task_id=generate_task_id,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=solution_task_id,
                    predecessor_task_id=main_task_id,
                    task_kind="solution-run",
                    source_path="solutions/wa.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="wrong_answer",
                ),
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result("OK"),
                )
                for task_id in (generate_task_id, main_task_id)
            ],
            leased=[(solution_task_id, solution_run_id, "jt-solution-001")],
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
        self.assertIn('data-test-name="001.in"', html)
        self.assertIn("running", html)

    def test_run_details_task_graph_ignores_stale_summary_test_cells(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-verif-task-stale-summary-{uuid.uuid4().hex[:8]}")
        solution_run_id = f"r-stale-summary-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-stale-summary"),
            activate_tasks=False,
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
                "signature": workspace_fingerprint_module.verification_sources_signature(ws),
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
        generate_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "generator-0",
                test_name,
            )
            for test_name in ("001.in", "002.in")
        }
        solution_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "solution-0",
                test_name,
            )
            for test_name in ("001.in", "002.in")
        }
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="generate-input",
                        source_path="generators/gen.cpp",
                        program_id="generator-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    )
                    for test_name, task_id in generate_task_ids.items()
                ],
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="solution-run",
                        source_path="solutions/accepted.cpp",
                        program_id="solution-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    )
                    for test_name, task_id in solution_task_ids.items()
                ],
            ],
            completions=[
                *[
                    TaskCompletion(
                        task_id=task_id,
                        status=VerificationTaskStore.TASK_DONE,
                        run_id="",
                        judgehost_task_id="",
                        result=execution_result("OK"),
                    )
                    for task_id in generate_task_ids.values()
                ],
                TaskCompletion(
                    task_id=solution_task_ids["001.in"],
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=solution_run_id,
                    judgehost_task_id="",
                    result=execution_result(
                        "AC",
                        runtime_sec=0.001,
                        cpu_sec=0.001,
                        wall_sec=0.001,
                        memory_kb=1024,
                    ),
                ),
            ],
            queued=[
                (
                    solution_task_ids["002.in"],
                    solution_run_id,
                    "jt-stale-summary-002",
                )
            ],
        )
        page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={verification_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("001.in", html)
        self.assertIn("AC", html)
        self.assertNotIn("9999", html)

    def test_run_details_task_graph_shows_generate_status_from_task_rows(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-verif-task-generate-{uuid.uuid4().hex[:8]}")
        solution_run_id = f"r-solution-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-generate"),
            activate_tasks=False,
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
        generate_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        solution_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=generate_task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="generators/gen.cpp",
                    program_id="generator-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=solution_task_id,
                    predecessor_task_id=generate_task_id,
                    task_kind="solution-run",
                    source_path="solutions/wa.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="wrong_answer",
                ),
            ],
            leased=[(generate_task_id, "generate-001", "jt-generate-001")],
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
        verification_id = canonical_test_verification_id(f"inv-verif-task-solution-status-{uuid.uuid4().hex[:8]}")
        solution_run_id = f"r-solution-status-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-solution-status"),
            activate_tasks=False,
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
        generate_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "generator-0",
                test_name,
            )
            for test_name in ("001.in", "002.in")
        }
        solution_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "solution-0",
                test_name,
            )
            for test_name in ("001.in", "002.in")
        }
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="generate-input",
                        source_path="generators/gen.cpp",
                        program_id="generator-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    )
                    for test_name, task_id in generate_task_ids.items()
                ],
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="solution-run",
                        source_path="solutions/wa.cpp",
                        program_id="solution-0",
                        test_name=test_name,
                        expected_behavior="wrong_answer",
                    )
                    for test_name, task_id in solution_task_ids.items()
                ],
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result("OK"),
                )
                for task_id in generate_task_ids.values()
            ],
            leased=[
                (
                    solution_task_ids["001.in"],
                    solution_run_id,
                    "jt-solution-001",
                )
            ],
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

    def test_run_details_task_graph_keeps_cancelled_solution_columns_visible_after_failed_cancel(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "cancelled.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-verif-task-cancelled-column-{uuid.uuid4().hex[:8]}")
        accepted_run_id = f"r-accepted-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verif-task-cancelled-column"),
            activate_tasks=False,
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
        generate_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "generator-0",
                test_name,
            )
            for test_name in ("001.in", "002.in")
        }
        accepted_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "solution-0",
                test_name,
            )
            for test_name in ("001.in", "002.in")
        }
        cancelled_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "solution-1",
                test_name,
            )
            for test_name in ("001.in", "002.in")
        }
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="generate-input",
                        source_path="generators/gen.cpp",
                        program_id="generator-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    )
                    for test_name, task_id in generate_task_ids.items()
                ],
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="solution-run",
                        source_path="solutions/accepted.cpp",
                        program_id="solution-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    )
                    for test_name, task_id in accepted_task_ids.items()
                ],
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="solution-run",
                        source_path="solutions/cancelled.cpp",
                        program_id="solution-1",
                        test_name=test_name,
                        expected_behavior="wrong_answer",
                    )
                    for test_name, task_id in cancelled_task_ids.items()
                ],
            ],
            completions=[
                *[
                    TaskCompletion(
                        task_id=task_id,
                        status=VerificationTaskStore.TASK_DONE,
                        run_id="",
                        judgehost_task_id="",
                        result=execution_result("OK"),
                    )
                    for task_id in generate_task_ids.values()
                ],
                TaskCompletion(
                    task_id=accepted_task_ids["001.in"],
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=accepted_run_id,
                    judgehost_task_id="",
                    result=execution_result("AC"),
                ),
                *[
                    TaskCompletion(
                        task_id=task_id,
                        status=VerificationTaskStore.TASK_CANCELLED,
                        run_id="",
                        judgehost_task_id="",
                        result=normalize_execution_result(
                            error="verification cancelled"
                        ),
                        fail_reason="verification cancelled",
                    )
                    for task_id in (
                        accepted_task_ids["002.in"],
                        *cancelled_task_ids.values(),
                    )
                ],
            ],
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

    def test_run_details_show_cancelled_main_correct_cells_for_failed_verification(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "cancelled.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-verif-main-cancel-{uuid.uuid4().hex[:8]}")
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-main-cancel"),
            activate_tasks=False,
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
        test_names = ("001.in", "002.in")
        generate_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "generator-0",
                test_name,
            )
            for test_name in test_names
        }
        main_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "accepted",
                test_name,
            )
            for test_name in test_names
        }
        solution_task_ids = {
            test_name: verification_task_id(
                verification_id,
                "solution-0",
                test_name,
            )
            for test_name in test_names
        }
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="generate-input",
                        source_path="generators/gen.cpp",
                        program_id="generator-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    )
                    for test_name, task_id in generate_task_ids.items()
                ],
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=generate_task_ids[test_name],
                        task_kind="main-correct",
                        source_path="solutions/accepted.cpp",
                        program_id="accepted",
                        test_name=test_name,
                        expected_behavior="accepted",
                    )
                    for test_name, task_id in main_task_ids.items()
                ],
                *[
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=main_task_ids[test_name],
                        task_kind="solution-run",
                        source_path="solutions/cancelled.cpp",
                        program_id="solution-0",
                        test_name=test_name,
                        expected_behavior="wrong_answer",
                    )
                    for test_name, task_id in solution_task_ids.items()
                ],
            ],
            completions=[
                *[
                    TaskCompletion(
                        task_id=task_id,
                        status=VerificationTaskStore.TASK_DONE,
                        run_id="",
                        judgehost_task_id="",
                        result=execution_result("OK"),
                    )
                    for task_id in generate_task_ids.values()
                ],
                *[
                    TaskCompletion(
                        task_id=task_id,
                        status=VerificationTaskStore.TASK_CANCELLED,
                        run_id="",
                        judgehost_task_id="",
                        result=normalize_execution_result(
                            error="verification cancelled by user"
                        ),
                        fail_reason="verification cancelled by user",
                    )
                    for task_id in (
                        *main_task_ids.values(),
                        *solution_task_ids.values(),
                    )
                ],
            ],
        )
        page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={verification_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("accepted.cpp", html)
        self.assertIn("cancelled", html)

    def test_verification_start_stays_queued_until_activation(self) -> None:
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
            row = config.verification_service.list_visible_verification_rows(
                problem_id,
                workspace_id,
                limit=1,
            )
            self.assertIsNotNone(row)
            assert row
            self.assertEqual(str(row[0]["status"] or ""), "queued")
        finally:
            fake_worker.stop()
            with config.verification_lock:
                config.verification_inflight.discard(workspace_key)
                config.verification_workers.discard(fake_worker)

    def test_run_details_uses_top_level_error_when_verification_fails_before_any_stage_starts(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "also-accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-verif-top-error-{uuid.uuid4().hex[:8]}")
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

    def test_run_details_marks_run_solutions_failed_when_verification_failed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = canonical_test_verification_id(f"inv-verif-run-failed-{uuid.uuid4().hex[:8]}")
        run_id = f"r-verif-run-failed-{uuid.uuid4().hex[:8]}"
        build_id = canonical_test_verification_id(
            f"artifact-run-failed:{uuid.uuid4().hex}"
        )
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
        self.assertIn("accepted solution failed on 001.in", html)

    def test_run_details_marks_run_solutions_interrupted_when_cancelled(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = canonical_test_verification_id(f"inv-verif-run-cancel-{uuid.uuid4().hex[:8]}")
        run_id = f"r-verif-run-cancel-{uuid.uuid4().hex[:8]}"
        build_id = canonical_test_verification_id(
            f"artifact-run-cancel:{uuid.uuid4().hex}"
        )
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
        self.assertIn("verification cancelled by user", html)

    def test_run_details_marks_build_failed_verification_execution_as_skipped(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = canonical_test_verification_id(f"inv-verif-skip-{uuid.uuid4().hex[:8]}")
        run_id = f"r-verif-skip-{uuid.uuid4().hex[:8]}"
        build_id = canonical_test_verification_id(
            self.random_id("b-verif-skip")
        )
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

    def test_pass_fail_detail_exposes_every_pass(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "two-pass.cpp").write_text(
            "int main(){return 0;}\n",
            encoding="utf-8",
        )

        verification_id = canonical_test_verification_id(
            f"ver-pass-fail-detail-{uuid.uuid4().hex[:8]}"
        )
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={"mode": "pass-fail", "pass_limit": 2},
        )

        def store(role: str, payload: bytes) -> str:
            return config.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name="001.in",
                role=role,
                file_name=f"{role}.txt",
                payload=payload,
            )

        original_input_ref = store("input", b"original input\n")
        answer_ref = store("answer", b"canonical answer\n")
        first_input_ref = store("pass-one-input", b"original input\n")
        second_input_ref = store("pass-two-input", b"next pass input\n")
        first_output_ref = store("pass-one-output", b"first pass output\n")
        second_output_ref = store("pass-two-output", b"second pass output\n")
        common_ref = store("metadata", b"metadata\n")
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/two-pass.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="two-pass.cpp",
                    judgehost_task_id="",
                    input_ref=original_input_ref,
                    answer_ref=answer_ref,
                    result=normalize_execution_result(
                        passes=(
                            ExecutionPassResult(
                                number=1,
                                capture_status=CAPTURE_COMPLETE,
                                runresult="correct",
                                verdict="OK",
                                score_text="",
                                answer_correct=True,
                                usage=ExecutionUsage(0.002, 0.001, 0.002, 1024),
                                feedback="first pass feedback",
                                artifacts=PassArtifacts(
                                    input_ref=first_input_ref,
                                    output_ref=first_output_ref,
                                    stderr_ref=common_ref,
                                    system_ref=common_ref,
                                    judge_message_ref=common_ref,
                                    team_message_ref=common_ref,
                                    metadata_ref=common_ref,
                                    compare_metadata_ref=common_ref,
                                ),
                            ),
                            ExecutionPassResult(
                                number=2,
                                capture_status=CAPTURE_COMPLETE,
                                runresult="wrong-answer",
                                verdict="WA",
                                score_text="",
                                answer_correct=False,
                                usage=ExecutionUsage(0.003, 0.002, 0.003, 1536),
                                feedback="second pass feedback",
                                artifacts=PassArtifacts(
                                    input_ref=second_input_ref,
                                    output_ref=second_output_ref,
                                    stderr_ref=common_ref,
                                    system_ref=common_ref,
                                    judge_message_ref=common_ref,
                                    team_message_ref=common_ref,
                                    metadata_ref=common_ref,
                                    compare_metadata_ref=common_ref,
                                ),
                            ),
                        )
                    ),
                )
            ],
        )

        detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=001.in&program_id=solution-0",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        html = detail.body.decode("utf-8", errors="replace")
        for evidence in (
            "canonical answer",
            "original input",
            "first pass feedback",
            "first pass output",
            "next pass input",
            "second pass feedback",
            "second pass output",
        ):
            self.assertIn(evidence, html)
        self.assertLess(html.index("first pass feedback"), html.index("second pass feedback"))

    def test_interactive_detail_uses_persisted_mode_and_exposes_every_pass(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "interactive.cpp").write_text(
            "int main(){return 0;}\n",
            encoding="utf-8",
        )
        (workspace / "solutions" / "other.cpp").write_text(
            "int main(){return 0;}\n",
            encoding="utf-8",
        )

        verification_id = canonical_test_verification_id(
            f"ver-interactive-detail-{uuid.uuid4().hex[:8]}"
        )
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={"mode": "interactive"},
        )

        def store(role: str, payload: bytes) -> str:
            return config.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name="001.in",
                role=role,
                file_name=f"{role}.bin",
                payload=payload,
            )

        input_ref = store("input", b"testcase seed\n")
        answer_ref = store("answer", b"accepted\n")
        input_one_ref = store("pass-one-input", b"first pass input\n")
        input_two_ref = store("pass-two-input", b"second pass input\n")
        common_ref = store("metadata", b"metadata\n")
        jury_one_ref = store("jury-one", b"first pass accepted\n")
        jury_two_ref = store("jury-two", b"second pass accepted\n")
        transcript_one = (
            b"[  0.019s/5]>: ping\n\n"
            b"[  0.024s/4]<: pong\n"
            b"[  0.025s/0]]"
        )
        transcript_two = b"[  0.031s/5]>: final\n" + b"broken"
        transcript_one_ref = store("transcript-one", transcript_one)
        transcript_two_ref = store("transcript-two", transcript_two)
        other_transcript_ref = store("transcript-other", b"[  0.001s/4]>: trap\n")
        other_jury_ref = store("jury-other", b"must not be read\n")
        passes = (
            ExecutionPassResult(
                number=1,
                capture_status=CAPTURE_COMPLETE,
                runresult="correct",
                verdict="OK",
                score_text="",
                answer_correct=True,
                usage=ExecutionUsage(0.024, 0.020, 0.024, 1024),
                feedback="",
                artifacts=PassArtifacts(
                    input_ref=input_one_ref,
                    transcript_ref=transcript_one_ref,
                    stderr_ref=common_ref,
                    system_ref=common_ref,
                    judge_message_ref=jury_one_ref,
                    team_message_ref=common_ref,
                    metadata_ref=common_ref,
                    compare_metadata_ref=common_ref,
                ),
            ),
            ExecutionPassResult(
                number=2,
                capture_status=CAPTURE_COMPLETE,
                runresult="correct",
                verdict="OK",
                score_text="",
                answer_correct=True,
                usage=ExecutionUsage(0.031, 0.027, 0.031, 1536),
                feedback="",
                artifacts=PassArtifacts(
                    input_ref=input_two_ref,
                    transcript_ref=transcript_two_ref,
                    stderr_ref=common_ref,
                    system_ref=common_ref,
                    judge_message_ref=jury_two_ref,
                    team_message_ref=common_ref,
                    metadata_ref=common_ref,
                    compare_metadata_ref=common_ref,
                ),
            ),
            ExecutionPassResult(
                number=3,
                capture_status=CAPTURE_METADATA_ONLY,
                runresult="correct",
                verdict="OK",
                score_text="",
                answer_correct=True,
                usage=ExecutionUsage(0.034, 0.029, 0.034, 1536),
                feedback="",
                artifacts=PassArtifacts(
                    metadata_ref=common_ref,
                    compare_metadata_ref=common_ref,
                ),
            ),
        )
        other_result = normalize_execution_result(
            passes=(
                ExecutionPassResult(
                    number=1,
                    capture_status=CAPTURE_COMPLETE,
                    runresult="correct",
                    verdict="OK",
                    score_text="",
                    answer_correct=True,
                    usage=ExecutionUsage(0.001, 0.001, 0.001, 512),
                    feedback="",
                    artifacts=PassArtifacts(
                        input_ref=input_ref,
                        transcript_ref=other_transcript_ref,
                        stderr_ref=common_ref,
                        system_ref=common_ref,
                        judge_message_ref=other_jury_ref,
                        team_message_ref=common_ref,
                        metadata_ref=common_ref,
                        compare_metadata_ref=common_ref,
                    ),
                ),
            )
        )
        interactive_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        other_task_id = verification_task_id(
            verification_id,
            "solution-1",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=interactive_task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/interactive.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=other_task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/other.cpp",
                    program_id="solution-1",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ],
            completions=[
                TaskCompletion(
                    task_id=interactive_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="interactive.cpp",
                    judgehost_task_id="",
                    result=normalize_execution_result(passes=passes),
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                ),
                TaskCompletion(
                    task_id=other_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="other.cpp",
                    judgehost_task_id="",
                    result=other_result,
                ),
            ],
        )

        full_detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=001.in",
            ),
            "alice/sample",
            "alice",
        )
        full_html = full_detail.body.decode("utf-8", errors="replace")
        self.assertIn("first pass accepted", full_html)
        self.assertIn("must not be read", full_html)
        self.assertIn("trap", full_html)

        detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=001.in&program_id=solution-0",
            ),
            "alice/sample",
            "alice",
        )

        self.assertEqual(detail.status_code, 200)
        html = detail.body.decode("utf-8", errors="replace")
        for evidence in (
            "first pass input",
            "second pass input",
            "first pass accepted",
            "second pass accepted",
            "closed output",
        ):
            self.assertIn(evidence, html)
        self.assertNotIn("testcase seed", html)
        self.assertNotIn("third pass input", html)
        self.assertNotIn("must not be read", html)
        self.assertNotIn("trap", html)

        with self.assertRaises(HTTPException) as unknown_run:
            run_details_test_fragment(
                _request(
                    "/problems/alice/sample/run/details/test-fragment",
                    f"verification_id={verification_id}&test=001.in&program_id=unknown",
                ),
                "alice/sample",
                "alice",
            )
        self.assertEqual(unknown_run.exception.status_code, 404)

        db_execute("UPDATE verifications SET mode='' WHERE id=?", [verification_id])
        malformed = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=001.in&program_id=solution-0",
            ),
            "alice/sample",
            "alice",
        )
        malformed_html = malformed.body.decode("utf-8", errors="replace")
        self.assertIn("no valid persisted mode", malformed_html)
        self.assertNotIn("transcript-event-interactor", malformed_html)

    def test_run_details_exposes_compile_diagnostic(self) -> None:
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
        self.assertIn("validator.cpp:4:35", html)
        self.assertIn("expected &#39;;&#39; before &#39;inf&#39;", html)

    def test_run_details_reads_runtime_inputs_answers_and_column_outputs_for_task_graph(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "tmp.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (workspace / "solutions" / "other.cpp").write_text(
            "int main(){return 0;}\n",
            encoding="utf-8",
        )

        verification_id = canonical_test_verification_id(f"ver-runtime-detail-{uuid.uuid4().hex[:8]}")
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={"mode": "pass-fail"},
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
        other_output_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="other-output",
            file_name="other.out",
            payload=b"other output\n",
            extra_tags={"run_id": "other.cpp"},
        )
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        other_task_id = verification_task_id(
            verification_id,
            "solution-1",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/tmp.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=other_task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/other.cpp",
                    program_id="solution-1",
                    test_name="001.in",
                    expected_behavior="wrong_answer",
                ),
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="tmp.cpp",
                    judgehost_task_id="",
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                    result=execution_result(
                        "OK",
                        runtime_sec=0.003,
                        cpu_sec=0.002,
                        wall_sec=0.003,
                        memory_kb=1024,
                        output_ref=output_ref,
                    ),
                ),
                TaskCompletion(
                    task_id=other_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="other.cpp",
                    judgehost_task_id="",
                    result=execution_result(
                        "WA",
                        runtime_sec=0.004,
                        cpu_sec=0.003,
                        wall_sec=0.004,
                        memory_kb=2048,
                        feedback="expected 6",
                        output_ref=other_output_ref,
                    ),
                ),
            ],
        )

        full_detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=001.in",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(full_detail.status_code, 200)
        full_html = full_detail.body.decode("utf-8", errors="replace")
        self.assertIn("other output", full_html)
        self.assertIn("expected 6", full_html)

        detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=001.in&program_id=solution-0",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertNotIn("other output", detail_html)
        self.assertIn("1 2 3", detail_html)
        self.assertIn("6", detail_html)
        self.assertIn(
            f"/problems/alice/sample/artifacts/{verification_id}/tests/001.in",
            detail_html,
        )
        self.assertIn(
            f"/problems/alice/sample/artifacts/{verification_id}/ans/001.ans",
            detail_html,
        )
        self.assertIn(f"/problems/alice/sample/artifacts/{verification_id}/blob/", detail_html)
        self.assertNotIn("(output missing)", detail_html)
        input_download = artifact_file(
            "alice/sample",
            "alice",
            verification_id,
            "tests/001.in",
        )
        self.assertEqual(input_download.status_code, 200)
        self.assertEqual(Path(str(input_download.path)).read_bytes(), b"1 2 3\n")
        answer_download = artifact_file(
            "alice/sample",
            "alice",
            verification_id,
            "ans/001.ans",
        )
        self.assertEqual(answer_download.status_code, 200)
        self.assertEqual(Path(str(answer_download.path)).read_bytes(), b"6\n")

    def test_uploaded_program_is_selected_and_queryable_by_program_id(self) -> None:
        ctx = workspace_service.workspace_context(
            "alice/sample",
            "alice",
            include_recent=False,
        )
        verification_id = canonical_test_verification_id(
            f"ver-uploaded-program-{uuid.uuid4().hex[:8]}"
        )
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            kind=Kind.CUSTOM,
            detail={"mode": "pass-fail"},
        )
        output_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="output",
            file_name="001.out",
            payload=b"uploaded output\n",
        )
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="uploads/solution-0/foo.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="unknown",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="uploaded-foo",
                    judgehost_task_id="",
                    result=execution_result("OK", output_ref=output_ref),
                )
            ],
        )

        page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={verification_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertIn("foo.cpp", page_html)
        self.assertNotIn("path=uploads", page_html)

        detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                (
                    f"verification_id={verification_id}&test=001.in"
                    "&program_id=solution-0"
                ),
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("foo.cpp", detail_html)
        self.assertIn("uploaded output", detail_html)

    def test_collaborator_can_view_foreign_workspace_run_details(self) -> None:
        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        workspace_service.ensure_workspace("alice/sample", "bob")

        alice_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-collab-detail-{uuid.uuid4().hex[:8]}")
        artifact_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=alice_workspace_id,
            build_id=verification_id,
            activate_tasks=False,
            kind=Kind.ALL,
            status="ok",
            created_at="2026-05-04T00:00:00Z",
            finished_at="2026-05-04T00:00:01Z",
            runs=[
                {
                    "id": "r-collab-detail",
                    "status": "ok",
                    "artifact_path": str(artifact_root),
                    "source_label": "solutions/std.cpp",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/std.cpp",
                        "tests": [],
                        "compile_log": "",
                        "compile_diagnostics": [],
                    },
                }
            ],
            summary_extra={
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "kind": "manual",
                        "id": "001",
                        "sample": False,
                        "sample_input_custom": False,
                        "sample_output_custom": False,
                        "sample_output_validate": False,
                        "desc": "",
                        "source": "",
                    }
                ],
            },
        )
        output_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="output",
            file_name="001.out",
            payload=b"6\n",
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
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/std.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="std.cpp",
                    judgehost_task_id="",
                    result=execution_result(
                        "OK",
                        runtime_sec=0.003,
                        cpu_sec=0.002,
                        wall_sec=0.003,
                        memory_kb=1024,
                        output_ref=output_ref,
                    ),
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                )
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "bob",
        )
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertIn("std.cpp", page_html)

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in&program_id=solution-0"),
            "alice/sample",
            "bob",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("Input 001.in", detail_html)
        self.assertIn("Answer", detail_html)

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

        verification_id = canonical_test_verification_id(f"ver-generate-detail-{uuid.uuid4().hex[:8]}")
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={
                "mode": "pass-fail",
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "kind": "gen",
                        "source": "generators/random_tree.cpp",
                        "command": 'random_tree 10 <20> & "quoted"',
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
        generate_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        solution_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=generate_task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="generators/random_tree.cpp",
                    program_id="generator-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=solution_task_id,
                    predecessor_task_id=generate_task_id,
                    task_kind="solution-run",
                    source_path="solutions/tmp.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ],
            completions=[
                TaskCompletion(
                    task_id=generate_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result("AC", feedback="tree is valid"),
                ),
                TaskCompletion(
                    task_id=solution_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="tmp.cpp",
                    judgehost_task_id="",
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                    result=execution_result(
                        "OK",
                        runtime_sec=0.003,
                        cpu_sec=0.002,
                        wall_sec=0.003,
                        memory_kb=1024,
                        output_ref=output_ref,
                    ),
                ),
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertIn('data-test-source-kind="generated"', page_html)
        self.assertIn('data-test-command="random_tree 10 &lt;20&gt; &amp; &#34;quoted&#34;"', page_html)

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in&program_id=solution-0"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertNotIn("tree is valid", detail_html)
        self.assertIn("2ms (3ms wall)", detail_html)
        self.assertIn("1MB", detail_html)

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

        verification_id = canonical_test_verification_id(f"ver-generate-fail-{uuid.uuid4().hex[:8]}")
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={
                "mode": "pass-fail",
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "kind": "gen",
                        "source": "generators/random_tree.cpp",
                        "command": "random_tree 10 20",
                    }
                ],
            },
        )
        generate_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=generate_task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="generators/random_tree.cpp",
                    program_id="generator-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=generate_task_id,
                    status=VerificationTaskStore.TASK_FAILED,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result(
                        "FL",
                        error="validator rejected generated test",
                    ),
                    fail_reason="validator rejected generated test",
                )
            ],
        )

        page = run_details_page(_request("/problems/alice/sample/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        test_link = re.search(
            r'(?s)<td class="tcell tone-fail"[^>]*>\s*(<a href="#run-test-detail-popup" data-popup-open="run-test-detail-popup" data-test-name="001\.in" data-test-source-kind="generated" data-test-command="random_tree 10 20"[^>]*>001\.in</a>)',
            page_html,
        )
        self.assertIsNotNone(test_link)

        detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=001.in",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("validator rejected generated test", detail_html)

    def test_run_test_detail_fragment_hides_manual_validate_placeholder_source(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])

        verification_id = canonical_test_verification_id(f"ver-manual-generate-{uuid.uuid4().hex[:8]}")
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={
                "mode": "pass-fail",
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
        generate_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        solution_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=generate_task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="manual_validate.cpp",
                    program_id="generator-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=solution_task_id,
                    predecessor_task_id=generate_task_id,
                    task_kind="solution-run",
                    source_path="solutions/tmp.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ],
            completions=[
                TaskCompletion(
                    task_id=generate_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result(
                        "AC",
                        feedback="manual input valid",
                    ),
                )
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            page_html,
            r'(?s)<td class="tcell tone-ok"[^>]*>\s*<a href="#run-test-detail-popup" data-popup-open="run-test-detail-popup" data-test-name="001\.in" data-test-source-kind="manual" data-test-command=""[^>]*>001\.in</a>',
        )

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in&program_id=solution-0"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertNotIn("manual_validate.cpp", detail_html)

    def test_run_details_expose_duplicate_generation_diagnostics(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "tmp.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        verification_id = canonical_test_verification_id(f"ver-duplicate-detail-{uuid.uuid4().hex[:8]}")
        tests_meta_rows = [
            {
                "index": index,
                "test_name": f"{index:03d}.in",
                "kind": "gen",
                "source": "generators/gen.cpp",
                "command": f"gen {index}",
            }
            for index in range(1, 7)
        ]
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={"mode": "pass-fail", "tests_meta_rows": tests_meta_rows},
        )

        def generation_fixture(
            test_name: str,
            *,
            status: str,
            verdict: str,
            output_ref: str = "",
            error_text: str = "",
            feedback_text: str = "",
        ) -> tuple[PlannedTask, TaskCompletion]:
            task_id = verification_task_id(
                verification_id,
                "generator-0",
                test_name,
            )
            return (
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path="generators/gen.cpp",
                    program_id="generator-0",
                    test_name=test_name,
                    expected_behavior="accepted",
                ),
                TaskCompletion(
                    task_id=task_id,
                    status=status,
                    run_id="generator",
                    judgehost_task_id="",
                    result=execution_result(
                        verdict,
                        output_ref=output_ref,
                        error=error_text,
                        feedback=feedback_text,
                    ),
                    fail_reason=(
                        error_text or feedback_text
                        if status
                        in {
                            VerificationTaskStore.TASK_FAILED,
                            VerificationTaskStore.TASK_CANCELLED,
                        }
                        else ""
                    ),
                ),
            )

        generation_fixtures = [
            generation_fixture(
                "001.in",
                status=VerificationTaskStore.TASK_DONE,
                verdict="AC",
                output_ref="blob://exact",
            ),
            generation_fixture(
                "002.in",
                status=VerificationTaskStore.TASK_DONE,
                verdict="SK",
                output_ref="blob://exact",
                feedback_text="duplicate generator invocation; skipped, same as 001.in",
            ),
            generation_fixture(
                "003.in",
                status=VerificationTaskStore.TASK_DONE,
                verdict="AC",
                output_ref="blob://content",
            ),
            generation_fixture(
                "004.in",
                status=VerificationTaskStore.TASK_DONE,
                verdict="SK",
                output_ref="blob://content",
                feedback_text="duplicate generated input; skipped, same as 003.in",
            ),
            generation_fixture(
                "005.in",
                status=VerificationTaskStore.TASK_DONE,
                verdict="SK",
                output_ref="blob://unresolved",
            ),
            generation_fixture(
                "006.in",
                status=VerificationTaskStore.TASK_CANCELLED,
                verdict="",
                error_text="generation cancelled by operator",
            ),
        ]
        planned_tasks = [fixture[0] for fixture in generation_fixtures]
        completions = [fixture[1] for fixture in generation_fixtures]
        for index in range(1, 7):
            is_duplicate = index in {2, 4, 5}
            test_name = f"{index:03d}.in"
            task_id = verification_task_id(
                verification_id,
                "solution-0",
                test_name,
            )
            planned_tasks.append(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/tmp.cpp",
                    program_id="solution-0",
                    test_name=test_name,
                    expected_behavior="accepted",
                )
            )
            completions.append(
                TaskCompletion(
                    task_id=task_id,
                    status=(
                        VerificationTaskStore.TASK_CANCELLED
                        if index == 6
                        else VerificationTaskStore.TASK_DONE
                    ),
                    run_id="tmp-solution",
                    judgehost_task_id="",
                    result=execution_result(
                        "SK" if is_duplicate else "OK",
                        output_ref="" if is_duplicate else f"blob://solution-{index}",
                    ),
                    fail_reason=(
                        "generation cancelled by operator" if index == 6 else ""
                    ),
                )
            )
        self._activate_verification_fixture(
            verification_id,
            tasks=planned_tasks,
            completions=completions,
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertIn(
            "002.in duplicate of 001.in; 004.in duplicate of 003.in; 005.in skipped (duplicate owner unavailable)",
            page_html,
        )

        duplicate_detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=004.in&program_id=solution-0",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(duplicate_detail.status_code, 200)
        duplicate_html = duplicate_detail.body.decode("utf-8", errors="replace")
        self.assertIn("duplicate of 003.in", duplicate_html)
        self.assertIn("Input 004.in", duplicate_html)
        self.assertIn("Answer", duplicate_html)

        cancelled_detail = run_details_test_fragment(
            _request(
                "/problems/alice/sample/run/details/test-fragment",
                f"verification_id={verification_id}&test=006.in&program_id=solution-0",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(cancelled_detail.status_code, 200)
        cancelled_html = cancelled_detail.body.decode("utf-8", errors="replace")
        self.assertIn("generation cancelled by operator", cancelled_html)

    def test_run_details_default_limit_keeps_213_solution_results_visible(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "bulk.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        verification_id = canonical_test_verification_id(f"ver-detail-limit-{uuid.uuid4().hex[:8]}")
        tests_meta_rows = [
            {
                "index": index,
                "test_name": f"{index:03d}.in",
                "kind": "gen",
                "source": "generators/gen.cpp",
                "command": f"gen {index}",
            }
            for index in range(1, 214)
        ]
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={"mode": "pass-fail", "tests_meta_rows": tests_meta_rows},
        )
        tasks: list[PlannedTask] = []
        completions: list[TaskCompletion] = []
        for index in range(1, 214):
            test_name = f"{index:03d}.in"
            generate_task_id = verification_task_id(
                verification_id,
                "generator-0",
                test_name,
            )
            solution_task_id = verification_task_id(
                verification_id,
                "solution-0",
                test_name,
            )
            tasks.extend(
                [
                    PlannedTask(
                        task_id=generate_task_id,
                        predecessor_task_id=None,
                        task_kind="generate-input",
                        source_path="generators/gen.cpp",
                        program_id="generator-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    ),
                    PlannedTask(
                        task_id=solution_task_id,
                        predecessor_task_id=generate_task_id,
                        task_kind="solution-run",
                        source_path="solutions/bulk.cpp",
                        program_id="solution-0",
                        test_name=test_name,
                        expected_behavior="accepted",
                    ),
                ]
            )
            completions.extend(
                [
                    TaskCompletion(
                        task_id=generate_task_id,
                        status=VerificationTaskStore.TASK_DONE,
                        run_id="bulk-generator",
                        judgehost_task_id="",
                        result=execution_result(
                            "AC",
                            output_ref=f"blob://input-{index}",
                        ),
                    ),
                    TaskCompletion(
                        task_id=solution_task_id,
                        status=VerificationTaskStore.TASK_DONE,
                        run_id="bulk-solution",
                        judgehost_task_id="",
                        result=execution_result(
                            "OK",
                            runtime_sec=0.001,
                            cpu_sec=0.001,
                            wall_sec=0.001,
                            memory_kb=256,
                            output_ref=f"blob://output-{index}",
                        ),
                    ),
                ]
            )
        self._activate_verification_fixture(
            verification_id,
            tasks=tasks,
            completions=completions,
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertEqual(config.config_values.RUN_DETAIL_TEST_LIST_LIMIT, 999)
        visible_rows = set(
            re.findall(
                r'data-test-name="(20[5-9]\.in|21[0-3]\.in)" data-test-source-kind="generated" '
                r'data-test-command="gen (20[5-9]|21[0-3])"',
                page_html,
            )
        )
        self.assertEqual(
            {name for name, _command_index in visible_rows},
            {f"{index:03d}.in" for index in range(205, 214)},
        )
        self.assertEqual(
            {int(command_index) for _name, command_index in visible_rows},
            set(range(205, 214)),
        )
        self.assertNotIn("Showing first 200", page_html)

    def test_run_details_page_shows_main_correct_compile_diagnostics_text(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        problem_id = int(ctx["problem"]["id"])
        workspace = Path(str(ctx["workspace"]["path"]))
        (workspace / "solutions").mkdir(parents=True, exist_ok=True)
        (workspace / "solutions" / "std.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        verification_id = canonical_test_verification_id(f"ver-main-correct-diag-{uuid.uuid4().hex[:8]}")
        detailed_error = (
            "g++: internal compiler error: File size limit exceeded signal terminated program as\n"
            "Please submit a full bug report."
        )
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind=Kind.ALL,
            detail={
                "mode": "pass-fail",
                "tests_meta_rows": [
                    {
                        "index": 1,
                        "test_name": "001.in",
                        "source": "manual_validate.cpp",
                    }
                ],
            },
        )
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/std.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_FAILED,
                    run_id="r-main-correct-diag",
                    judgehost_task_id="",
                    result=normalize_execution_result(
                        verdict="CE",
                        error=detailed_error,
                        compile_log=detailed_error,
                        compile_diagnostics=(
                            {"level": "error", "message": detailed_error},
                        ),
                    ),
                    fail_reason=detailed_error,
                )
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("File size limit exceeded", html)
        self.assertIn("Please submit a full bug report.", html)

    def test_run_details_shows_sanity_diagnostics(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-sanity-popup-{uuid.uuid4().hex[:8]}")
        run_id = f"r-sanity-popup-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-sanity-popup"),
            activate_tasks=False,
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
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "003.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name="003.in",
                    expected_behavior="accepted",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=run_id,
                    judgehost_task_id="",
                    result=execution_result(
                        "WA",
                        runtime_sec=0.01,
                        cpu_sec=0.01,
                        wall_sec=0.01,
                        memory_kb=1,
                        output_ref="blob://sanity-popup",
                    ),
                )
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("verification-sanity-detail-popup", html)
        self.assertIn("Custom sample output", html)
        self.assertIn("validator reported mismatch", html)

    def test_run_details_sanity_warning_is_visible_without_failed_verification(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-sanity-warning-{uuid.uuid4().hex[:8]}")
        run_id = f"run-warning-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-sanity-warning"),
            activate_tasks=False,
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
                "error": "Test data did not hit: n max=3",
            },
        )
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=run_id,
                    judgehost_task_id="",
                    result=execution_result(
                        "OK",
                        runtime_sec=0.01,
                        cpu_sec=0.01,
                        wall_sec=0.01,
                        memory_kb=1,
                        output_ref="blob://sanity-warning",
                    ),
                )
            ],
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
        self.assertIn("Test data did not hit: n max=3", html)

    def test_run_details_sanity_failed_keeps_verification_status_ok(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-sanity-ok-failed-{uuid.uuid4().hex[:8]}")
        run_id = f"run-sanity-ok-failed-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-sanity-ok-failed"),
            activate_tasks=False,
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
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_verification_fixture(
            verification_id,
            tasks=[
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path="solutions/accepted.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                )
            ],
            completions=[
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=run_id,
                    judgehost_task_id="",
                    result=execution_result(
                        "OK",
                        runtime_sec=0.01,
                        cpu_sec=0.01,
                        wall_sec=0.01,
                        memory_kb=1,
                        output_ref="blob://sanity-failed",
                    ),
                )
            ],
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
        verification_id = canonical_test_verification_id(f"ver-runtime-threshold-{uuid.uuid4().hex[:8]}")
        slow_run_id = f"run-runtime-threshold-{uuid.uuid4().hex[:8]}"
        mixed_run_id = f"run-runtime-mixed-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-runtime-threshold"),
            activate_tasks=False,
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
        result_specs = [
            ("solutions/slow.cpp", slow_run_id, 0, "001.in", "OK", 0.6),
            ("solutions/slow.cpp", slow_run_id, 0, "002.in", "OK", 1.2),
            ("solutions/slow.cpp", slow_run_id, 0, "003.in", "OK", 0.2),
            ("solutions/mixed.cpp", mixed_run_id, 1, "001.in", "OK", 0.4),
            ("solutions/mixed.cpp", mixed_run_id, 1, "002.in", "TL", 1.2),
            ("solutions/mixed.cpp", mixed_run_id, 1, "003.in", "WA", 0.7),
        ]
        planned_tasks: list[PlannedTask] = []
        completions: list[TaskCompletion] = []
        for source_path, run_id, program_index, test_name, verdict, runtime_sec in result_specs:
            task_id = verification_task_id(
                verification_id,
                f"solution-{program_index}",
                test_name,
            )
            planned_tasks.append(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="solution-run",
                    source_path=source_path,
                    program_id=f"solution-{program_index}",
                    test_name=test_name,
                    expected_behavior="accepted",
                )
            )
            completions.append(
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=run_id,
                    judgehost_task_id="",
                    result=execution_result(
                        verdict,
                        cpu_sec=runtime_sec,
                        runtime_sec=runtime_sec,
                        wall_sec=runtime_sec,
                        memory_kb=1024,
                        output_ref=f"blob://runtime-{program_index}-{test_name}",
                    ),
                )
            )
        self._activate_verification_fixture(
            verification_id,
            tasks=planned_tasks,
            completions=completions,
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
        verification_id = canonical_test_verification_id(f"ver-sanity-list-{uuid.uuid4().hex[:8]}")
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
                "error": "Test data did not hit: n max=3",
            },
        )

        page = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("ok (has warning)", html)
        self.assertIn("Test data did not hit: n max=3", html)

    def test_workflow_pages_emit_files_source_context_links(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        ws = Path(str(ctx["workspace"]["path"]))
        statement_sig = statement_sources_signature(
            ws,
            problem_title=statement_title_for_language(
                ws,
                "english",
                fallback_title=Path(str(ctx["problem"]["slug"])).name,
            ),
            tests_spec_max_bytes=TEXTAREA_MAX_BYTES,
            statement_sample_max_bytes=STATEMENT_SAMPLE_MAX_BYTES,
        )

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
        verification_id = self._verification_id_for_run(run_id)
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
            artifact_path=str(
                config.fs_manager.prepare_verification_root(verification_id).resolve()
            ),
            created_at="2026-02-23T00:02:00Z",
            finished_at="2026-02-23T00:02:01Z",
        )
        run_resp = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        run_html = run_resp.body.decode("utf-8", errors="replace")
        self.assertIn(
            f"/problems/alice/sample/run/details?verification_id={verification_id}",
            run_html,
        )
        self.assertIn('action="/problems/alice/sample/verification/start"', run_html)
        self.assertIn('action="/problems/alice/sample/export/create"', run_html)

    def test_problem_nav_downloads_package_built_for_current_revision(self) -> None:
        workspace = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        marker = workspace / "notes" / f"package-nav-{uuid.uuid4().hex[:8]}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("package nav fixture\n", encoding="utf-8")
        with config.workspace_service.workspace_lock(workspace):
            head_commit = config.git_service.commit(
                workspace,
                "Prepare package navigation fixture",
                "alice",
                "alice@example.test",
            )
        self.assertTrue(head_commit)
        current_export = {
            "id": "job-current",
            "problem_id": 1,
            "actor_user_id": 1,
            "export_type": "icpc",
            "status": "succeeded",
            "materialization_id": "mat-current",
            "export_id": "exp current",
            "error": "",
            "filename": "sample current.zip",
            "sha256": "abc",
            "size_bytes": 123,
            "source_commit": head_commit,
            "created_at": "2026-08-08T00:00:00Z",
            "started_at": "2026-08-08T00:00:00Z",
            "finished_at": "2026-08-08T00:00:01Z",
        }
        with (
            patch.object(config.export_service, "latest_source_commit", return_value=head_commit),
            patch.object(config.export_service, "latest_succeeded_export_job", return_value=current_export),
            patch.object(config.export_service, "export_archive_path", return_value=Path("/tmp/sample-current.zip")),
        ):
            response = run_page(_request("/problems/alice/sample/run"), "alice/sample", "alice")
        html = response.body.decode("utf-8", errors="replace")
        self.assertIn(
            'href="/problems/alice/sample/exports/exp%20current/sample%20current.zip">Download</a>',
            html,
        )
        self.assertNotIn('action="/problems/alice/sample/export/create"', html)

    def test_packages_are_problem_visible_across_users_and_build_origins(self) -> None:
        from app.impl.run_export.artifact import materialization_file

        workspace_service.ensure_user("bob")
        workspace_service.grant_repo_access("alice/sample", "bob", "read")
        workspace_service.ensure_workspace("alice/sample", "bob", refresh_status=False)
        alice_ctx = workspace_service.workspace_context(
            "alice/sample",
            "alice",
            include_recent=False,
        )
        problem_id = int(alice_ctx["problem"]["id"])
        actor_user_id = int(alice_ctx["user"]["id"])
        materialization_id = f"pm-visible-{uuid.uuid4().hex[:8]}"
        source_commit = "9" * 40
        db_execute(
            """
            INSERT INTO problem_package_materializations(
                id,problem_id,source_commit,revision_number,source_digest,
                archive_rel_path,archive_sha256,archive_size_bytes,
                verification_id,status,created_at,checked_at,unavailable_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,'available',?,?,'')
            """,
            [
                materialization_id,
                problem_id,
                source_commit,
                9,
                "a" * 64,
                f"materializations/{problem_id}/{source_commit}/native.zip",
                "b" * 64,
                123,
                canonical_test_verification_id(
                    f"ver-package-visible-{uuid.uuid4().hex[:8]}"
                ),
                "2026-08-11T00:00:00Z",
                "2026-08-11T00:00:01Z",
            ],
        )
        export_job_id = f"exp-visible-{uuid.uuid4().hex[:8]}"
        config.export_service.create_export_job(
            job_id=export_job_id,
            problem_id=problem_id,
            actor_user_id=actor_user_id,
            export_type="icpc",
            source_commit=source_commit,
        )
        config.export_service.mark_export_job_failed(
            export_job_id,
            "package failed for alice",
        )

        page = export_page(
            _request("/problems/alice/sample/export"),
            "alice/sample",
            "bob",
        )
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("v9", html)
        self.assertIn("native.zip", html)
        self.assertIn(
            f"/problems/alice/sample/packages/{materialization_id}/native.zip",
            html,
        )
        self.assertIn("package failed for alice", html)
        materialization = config.problem_package_service.materialization(
            materialization_id
        )
        self.assertIsNotNone(materialization)
        archive = Path(config.settings.artifacts_root) / "fixture-native.zip"
        archive.write_bytes(b"native package")
        with patch.object(
            config.problem_package_service,
            "native_archive",
            return_value=(materialization, archive),
        ):
            response = materialization_file(
                "alice/sample",
                "bob",
                materialization_id,
            )
        self.assertEqual(Path(response.path), archive)
        self.assertIn(
            "sample-native-v9.zip",
            str(response.headers.get("content-disposition") or ""),
        )

    def test_run_verification_details_prefers_verification_record_over_audit(self) -> None:
        from app.impl.workspace.run_view_lifecycle_card import load_verification_detail_summary

        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = canonical_test_verification_id(
            self.random_id("b-ver-details")
        )
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
        verification_id = canonical_test_verification_id(f"inv-ver-details-{uuid.uuid4().hex[:8]}")
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

    def test_run_list_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-row-status")
        verification_id = canonical_test_verification_id(f"ver-stale-status-{uuid.uuid4().hex[:8]}")
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

    def test_run_details_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-detail-status")
        verification_id = canonical_test_verification_id(f"ver-detail-stale-{uuid.uuid4().hex[:8]}")
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

        page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={verification_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(verification_id, html)
        self.assertIn("failed", html)

    def test_sidebar_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-sidebar-status")
        verification_id = canonical_test_verification_id(f"ver-sidebar-stale-{uuid.uuid4().hex[:8]}")
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
        self.assertIn(verification_id, html)
        self.assertIn("failed", html)
