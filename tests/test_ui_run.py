from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_execute,
    db_fetch_one,
    verification_programs_for_tasks,
)
from tests.execution_result_helpers import execution_result

import asyncio
import io
import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from starlette.responses import Response

from app.config import CONFIG_REGISTRY
from app.service.problem.test_spec import dumps_default_tests_spec
from app.service.verification.workspace_fingerprint import verification_sources_signature
from tests.common import E2ETestBase, override_config_values
from tests.identity_helpers import canonical_test_verification_id
from tests.ui_support import (
    Path,
    UIHelpersMixin,
    _post_form_request,
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
    runtime,
    json,
    export_create,
    revision_commit,
    run_details_page,
    run_details_sample_json,
    run_details_test_fragment,
    run_execute,
    run_cancel,
    run_rejudge,
    run_page,
    uuid,
    verification_start,
    workspace_service,
)

import app.impl.workspace.context_job as workspace_context_job
import app.service.problem.readiness as problem_readiness_module
import app.service.verification.workspace_fingerprint as workspace_fingerprint_module
from app.service.problem.readiness import WorkspaceReadinessSubject
from app.service.execution.model import (
    CAPTURE_COMPLETE,
    CAPTURE_METADATA_ONLY,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
)
from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus
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

    @staticmethod
    def _edit_spec_request(
        *,
        problem: str,
        user: str,
        index: str,
        test_id: str,
        kind: str,
        sample: str,
        payload: str,
        sample_input: str | None = None,
        sample_output: str | None = None,
        sample_output_validate: list[str] | None = None,
        sample_format: str | None = None,
        sample_json: str | None = None,
    ) -> Response:
        form_data: dict[str, object] = {
            "index": index,
            "test_id": test_id,
            "kind": kind,
            "sample": sample,
            "payload": payload,
        }
        if sample_input is not None:
            form_data["sample_input"] = sample_input
        if sample_output is not None:
            form_data["sample_output"] = sample_output
        if sample_output_validate is not None:
            form_data["sample_output_validate"] = sample_output_validate
        if sample_format is not None:
            form_data["sample_format"] = sample_format
        if sample_json is not None:
            form_data["sample_json"] = sample_json
        request = _post_form_request(
            f"/problems/{problem}/tests/spec/edit",
            form_data,
        )
        return asyncio.run(
            tests_spec_edit(
                request=request,
                problem=problem,
                user=user,
                index=index,
                test_id=test_id,
                kind=kind,
                sample=sample,
                payload=payload,
                sample_output_validate=sample_output_validate,
            )
        )

    def test_tests_spec_edit_accepts_structured_sample_json(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
        added = tests_spec_add_manual(
            problem="alice/sample",
            user="alice",
            test_id="001",
            sample="1",
            manual_input="judge input\n",
        )
        self.assertEqual(added.status_code, 303)
        structured = {
            "presentation": "pair",
            "passes": [
                {"number": 1, "input": "first\n", "output": "one\n"},
                {"number": 2, "input": "second\n", "output": "two\n"},
            ],
        }

        response = self._edit_spec_request(
            problem="alice/sample",
            user="alice",
            index="1",
            test_id="001",
            kind="manual",
            sample="1",
            payload="judge input\n",
            sample_format="json",
            sample_json=json.dumps(structured),
        )

        self.assertEqual(response.status_code, 303)
        stored = json.loads(spec_path.read_text(encoding="utf-8"))["tests"][0]
        self.assertEqual(stored["sample_json"], structured)
        reset = self._edit_spec_request(
            problem="alice/sample",
            user="alice",
            index="1",
            test_id="001",
            kind="manual",
            sample="1",
            payload="judge input\n",
            sample_format="default",
        )

        self.assertEqual(reset.status_code, 303)
        reset_row = json.loads(spec_path.read_text(encoding="utf-8"))["tests"][0]
        self.assertNotIn("sample_json", reset_row)
        self.assertNotIn("sample_input", reset_row)
        self.assertNotIn("sample_output", reset_row)

    def test_tests_page_preserves_sparse_legacy_sample_entries(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        manual_dir = ws / "tests" / "manual"
        manual_dir.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(
            json.dumps(
                {
                    "tests": [
                        {"id": "001", "kind": "manual", "sample": True},
                        {
                            "id": "002",
                            "kind": "manual",
                            "sample": True,
                            "sample_input": "legacy input only\n",
                        },
                        {
                            "id": "003",
                            "kind": "manual",
                            "sample": True,
                            "sample_output": "legacy output only\n",
                            "sample_output_validate": False,
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        for test_id in ("001", "002", "003"):
            (manual_dir / f"{test_id}.in").write_text(
                f"judge input {test_id}\n", encoding="utf-8"
            )

        page = tests_page(
            _request("/problems/alice/sample/tests"), "alice/sample", "alice"
        )

        self.assertEqual(page.status_code, 200)
        editor = page.context["tests_editor"]
        rows = editor["rows"]
        self.assertEqual(
            [
                (
                    row["custom_sample_input"],
                    row["custom_sample_output"],
                    row["custom_sample_json"],
                )
                for row in rows
            ],
            [(False, False, False), (True, False, False), (False, True, False)],
        )

        for index, test_id in enumerate(("001", "002", "003"), start=1):
            response = self._edit_spec_request(
                problem="alice/sample",
                user="alice",
                index=str(index),
                test_id=test_id,
                kind="manual",
                sample="1",
                payload=f"updated judge input {test_id}\n",
            )
            self.assertEqual(response.status_code, 303)

        stored = json.loads(spec_path.read_text(encoding="utf-8"))["tests"]
        self.assertNotIn("sample_input", stored[0])
        self.assertNotIn("sample_output", stored[0])
        self.assertNotIn("sample_json", stored[0])
        self.assertEqual(stored[1]["sample_input"], "legacy input only\n")
        self.assertNotIn("sample_output", stored[1])
        self.assertEqual(stored[2]["sample_output"], "legacy output only\n")
        self.assertFalse(stored[2]["sample_output_validate"])
        self.assertTrue(all("sample_json" not in row for row in stored))

    def test_incomplete_structured_sample_is_atomic_for_legacy_repository(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")
        added = tests_spec_add_manual(
            problem="alice/sample",
            user="alice",
            test_id="001",
            sample="1",
            manual_input="judge input\n",
            sample_input="legacy input\n",
            sample_output="legacy output\n",
        )
        self.assertEqual(added.status_code, 303)
        before = spec_path.read_bytes()

        response = self._edit_spec_request(
            problem="alice/sample",
            user="alice",
            index="1",
            test_id="001",
            kind="manual",
            sample="1",
            payload="changed judge input\n",
            sample_format="json",
            sample_json=json.dumps(
                {
                    "presentation": "pair",
                    "passes": [{"number": 1, "input": "missing output\n"}],
                }
            ),
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(spec_path.read_bytes(), before)
        stored = json.loads(before)["tests"][0]
        self.assertEqual(stored["sample_input"], "legacy input\n")
        self.assertEqual(stored["sample_output"], "legacy output\n")
        self.assertNotIn("sample_json", stored)

    def _problem_readiness(
        self,
        *,
        problem_id: int,
        workspace_id: int,
        workspace_path: Path,
        dirty: bool = True,
    ) -> dict[str, object]:
        workspace_row = runtime.workspace_service.workspace_rows(
            [problem_id],
            runtime.workspace_service.known_user_id("alice"),
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
            "native_package_revision_number": None,
            "native_package_id": "",
            "status": "none",
            "missing_reason": "Package not built",
        }
        with patch.object(
            runtime.problem_package_service,
            "published_readiness",
            return_value=package,
        ):
            return dict(
                runtime.problem_readiness_service.readiness(
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
                    status=VerificationTaskStatus.DONE,
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
        planned_by_id = {task.task_id: task for task in canonical_tasks}
        for task_id, run_id, judgehost_task_id in [*(queued or []), *(leased or [])]:
            planned_task = planned_by_id[task_id]
            self.assertTrue(
                runtime.verification_task_store.bind_and_expose_judgehost_runtime(
                    task_id,
                    expected_verification_id=verification_id,
                    expected_program_id=planned_task.program_id,
                    expected_test_name=planned_task.test_name,
                    run_id=run_id,
                    judgehost_task_id=judgehost_task_id,
                    expose=lambda: None,
                )
            )
        for task_id, _run_id, _judgehost_task_id in leased or []:
            runtime.verification_task_store.set_task_leased(task_id)
        if canonical_completions:
            runtime.verification_task_store.commit_task_completions(
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
        verification_root = runtime.storage_layout.prepare_verification_root(verification_id).resolve()
        verification_root.mkdir(parents=True, exist_ok=True)
        existing_row = runtime.verification_service.verification_record(verification_id)
        existing_metadata: dict[str, object] = {}
        existing_created_at = ""
        existing_finished_at = ""
        existing_signature = ""
        if existing_row is not None:
            payload = runtime.verification_service.verification_detail(verification_id)
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
                        status=VerificationTaskStatus.DONE,
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
                record = runtime.verification_service.verification_record(verification_id)
                if record is not None and str(record["status"]) == "running":
                    runtime.verification_service.fail_verification(
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
            else runtime.storage_layout.prepare_verification_root(verification_id).resolve()
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
                    status=VerificationTaskStatus.DONE,
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
            failure = runtime.verification_service.fail_verification(
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

        edit_gen = self._edit_spec_request(
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

        edit_spec = self._edit_spec_request(
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

        edit_spec_checked = self._edit_spec_request(
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

    def test_tests_spec_edit_can_clear_custom_sample_text(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        spec_path.write_text(dumps_default_tests_spec(), encoding="utf-8")

        added = tests_spec_add_manual(
            problem="alice/sample",
            user="alice",
            test_id="001",
            sample="1",
            manual_input="judge input\n",
            sample_input="custom input\n",
            sample_output="custom output\n",
            sample_output_validate=["0", "1"],
        )
        self.assertEqual(added.status_code, 303)

        preserved = self._edit_spec_request(
            problem="alice/sample",
            user="alice",
            index="1",
            test_id="001",
            kind="manual",
            sample="1",
            payload="updated judge input\n",
            sample_output_validate=["0", "1"],
        )
        self.assertEqual(preserved.status_code, 303)

        preserved_payload = json.loads(spec_path.read_text(encoding="utf-8"))
        preserved_tests = preserved_payload.get("tests") or []
        self.assertEqual(preserved_tests[0].get("sample_input"), "custom input\n")
        self.assertEqual(preserved_tests[0].get("sample_output"), "custom output\n")

        updated = self._edit_spec_request(
            problem="alice/sample",
            user="alice",
            index="1",
            test_id="001",
            kind="manual",
            sample="1",
            payload="updated judge input\n",
            sample_input="",
            sample_output="",
            sample_output_validate=["0", "1"],
        )
        self.assertEqual(updated.status_code, 303)

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual(len(tests), 1)
        self.assertNotIn("sample_input", tests[0])
        self.assertNotIn("sample_output", tests[0])
        self.assertNotIn("sample_output_validate", tests[0])

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

        override_config_values(self, runtime.config_values, UPLOAD_MAX_BYTES=1024)
        uploaded = asyncio.run(
            tests_spec_payload_upload(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=self._FakeUpload(b"x" * 1025),
            )
        )
        self.assertEqual(uploaded.status_code, 303)
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

        override_config_values(self, runtime.config_values, UPLOAD_MAX_BYTES=1024)
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
        metadata = runtime.verification_service.verification_detail(verification_id)
        self.assertIsInstance(metadata, dict)
        self.assertEqual(str(metadata.get("mode") or ""), "pass-fail")

    def test_run_execute_passes_canonical_targets_to_queue_start(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self._configure_solution_fixtures(
            ws,
            ("accepted.cpp", "accepted"),
            ("wa.cpp", "wrong_answer"),
        )
        observed = {"checked": False}

        def _fake_start_verification_job(*args, **kwargs) -> bool:
            verification_id = str(kwargs.get("verification_id") or "")
            targets = list(kwargs.get("targets") or [])
            solution_program_ids = [
                str(item.get("program_id") or "")
                for item in targets
                if str(item.get("program_id") or "")
            ]
            self.assertEqual(len(solution_program_ids), 2)
            self.assertEqual(len(set(solution_program_ids)), 2)
            self.assertTrue(verification_id)
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
        problem_id = int(
            workspace_service.workspace_context(
                problem,
                "alice",
                include_recent=False,
            )["problem"]["id"]
        )
        before = db_fetch_one(
            "SELECT COUNT(*) AS count FROM verifications WHERE problem_id=?",
            [problem_id],
        )
        self.assertIsNotNone(before)

        start_resp = verification_start(problem=problem, user="alice", page="statement")
        self.assertEqual(start_resp.status_code, 303)
        after = db_fetch_one(
            "SELECT COUNT(*) AS count FROM verifications WHERE problem_id=?",
            [problem_id],
        )
        self.assertIsNotNone(after)
        self.assertEqual(int(after["count"]), int(before["count"]))

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

        with patch(
            "app.service.platform.fs.layout.StorageLayout.prepare_verification_layout",
            side_effect=RuntimeError("verification layout unavailable"),
        ):
            runtime.verification_workflow.run(
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
                    status=VerificationTaskStatus.DONE,
                    run_id=run_ok,
                    judgehost_task_id="",
                    result=execution_result("OK"),
                ),
                TaskCompletion(
                    task_id=rerun_wa_task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id=run_wa,
                    judgehost_task_id="",
                    result=execution_result("WA"),
                ),
            ],
        )
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

        detail_page = run_details_page(
            _request(
                "/problems/alice/sample/run/details",
                f"verification_id={published_id}",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail_page.status_code, 200)

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
        published_row = db_fetch_one(
            "SELECT status FROM verifications WHERE id=?",
            [running_published_id],
        )
        self.assertIsNotNone(published_row)
        self.assertEqual(str(published_row["status"]), "running")

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
            runtime.verification_service.verification_record(foreign_id)["status"],
            "running",
        )

    def test_run_cancel_marks_running_verification_cancelled(self) -> None:
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
        verification_row = db_fetch_one("SELECT status,finished_at FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or "").lower(), "cancelled")
        self.assertTrue(str(verification_row["finished_at"] or ""))
        rows = {
            str(row["id"]): row
            for row in runtime.verification_task_store.list_rows(verification_id)
        }
        self.assertEqual(
            str(rows[leased_task_id]["status"] or ""),
            VerificationTaskStatus.CANCELLED,
        )
        self.assertEqual(
            str(rows[pending_task_id]["status"] or ""),
            VerificationTaskStatus.CANCELLED,
        )

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
        verification_row = db_fetch_one("SELECT status FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or "").strip().lower(), "cancelled")
        rows = runtime.verification_task_store.list_rows(verification_id)
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "solution-0"
            ],
            [VerificationTaskStatus.CANCELLED, VerificationTaskStatus.CANCELLED],
        )
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "accepted"
            ],
            [VerificationTaskStatus.DONE],
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
        rows = runtime.verification_task_store.list_rows(verification_id)
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "solution-0"
            ],
            [VerificationTaskStatus.CANCELLED, VerificationTaskStatus.CANCELLED],
        )
        self.assertEqual(
            [
                str(row["status"] or "")
                for row in rows
                if str(row["program_id"] or "") == "accepted"
            ],
            [VerificationTaskStatus.DONE],
        )


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
                runtime.worker_queue_service,
                "submit",
                return_value=(fake_worker, True, "queued"),
            ):
                start_resp = verification_start(problem=problem, user="alice", page="statement")
            self.assertEqual(start_resp.status_code, 303)
            row = runtime.verification_service.list_visible_verification_rows(
                problem_id,
                workspace_id,
                limit=1,
            )
            self.assertIsNotNone(row)
            assert row
            self.assertEqual(str(row[0]["status"] or ""), "queued")
        finally:
            fake_worker.stop()
            with runtime.verification_lock:
                runtime.verification_inflight.discard(workspace_key)
                runtime.verification_workers.discard(fake_worker)

    def test_verification_queue_rejection_is_a_failure(self) -> None:
        problem = f"alice/verify-queue-rejection-{uuid.uuid4().hex[:8]}"
        workspace = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(
            f"queue-rejection-{uuid.uuid4().hex}"
        )

        with (
            patch.object(
                runtime.worker_queue_service,
                "submit",
                return_value=(None, False, "capacity"),
            ),
            self.assertRaisesRegex(RuntimeError, "queue rejected"),
        ):
            workspace_context_job.start_verification_job(
                runtime,
                problem,
                "alice",
                actor_user_id=int(ctx["user"]["id"]),
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_head=str(ctx["workspace"].get("head_commit") or ""),
                workspace_dirty=bool(ctx["workspace"].get("dirty")),
                targets=[],
                verification_id=verification_id,
                workspace_path=workspace,
            )

        row = db_fetch_one(
            "SELECT status,fail_reason FROM verifications WHERE id=?",
            [verification_id],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "failed")
        self.assertIn("queue rejected", str(row["fail_reason"]))

    def test_pass_fail_sample_json_exposes_every_pass(self) -> None:
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
            return runtime.verification_service.store_verification_blob(
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
                    status=VerificationTaskStatus.DONE,
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

        json_response = run_details_sample_json(
            _request(
                "/problems/alice/sample/run/details/sample-json",
                f"verification_id={verification_id}&test=001.in&program_id=solution-0",
            ),
            "alice/sample",
            "alice",
        )
        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(
            json.loads(json_response.body.decode("utf-8")),
            {
                "presentation": "pair",
                "passes": [
                    {
                        "number": 1,
                        "input": "original input\n",
                        "output": "first pass output\n",
                    },
                    {
                        "number": 2,
                        "input": "next pass input\n",
                        "output": "second pass output\n",
                    },
                ],
            },
        )

    def test_interactive_sample_json_isolated_by_program_id(self) -> None:
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
            return runtime.verification_service.store_verification_blob(
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
            + b"[  0.026s/1]>: x\n" * 998
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
                    status=VerificationTaskStatus.DONE,
                    run_id="interactive.cpp",
                    judgehost_task_id="",
                    result=normalize_execution_result(passes=passes),
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                ),
                TaskCompletion(
                    task_id=other_task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="other.cpp",
                    judgehost_task_id="",
                    result=other_result,
                ),
            ],
        )

        json_response = run_details_sample_json(
            _request(
                "/problems/alice/sample/run/details/sample-json",
                f"verification_id={verification_id}&test=001.in&program_id=solution-1",
            ),
            "alice/sample",
            "alice",
        )
        downloaded = json.loads(json_response.body.decode("utf-8"))
        self.assertEqual(
            downloaded,
            {
                "presentation": "interaction",
                "passes": [
                    {
                        "number": 1,
                        "events": [
                            {"source": "interactor", "content": "trap"},
                        ],
                    }
                ],
            },
        )

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

    def test_collaborator_can_view_foreign_workspace_run_details(self) -> None:
        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        workspace_service.ensure_workspace("alice/sample", "bob")

        alice_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-collab-detail-{uuid.uuid4().hex[:8]}")
        artifact_root = runtime.storage_layout.prepare_verification_root(verification_id).resolve()
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
        output_ref = runtime.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="output",
            file_name="001.out",
            payload=b"6\n",
        )
        input_ref = runtime.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="input",
            file_name="001.in",
            payload=b"1 2 3\n",
        )
        answer_ref = runtime.verification_service.store_verification_blob(
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
                    status=VerificationTaskStatus.DONE,
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

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in&program_id=solution-0"),
            "alice/sample",
            "bob",
        )
        self.assertEqual(detail.status_code, 200)

    def test_package_create_reuses_existing_current_package(self) -> None:
        context = workspace_service.workspace_context(
            "alice/sample",
            "alice",
            include_recent=False,
        )
        problem_id = int(context["problem"]["id"])
        native_package_id = "pm-current-package"
        readiness = {
            "problem_id": problem_id,
            "published_commit": "a" * 40,
            "published_revision_number": 3,
            "native_package_revision_number": 3,
            "native_package_id": native_package_id,
            "status": "ready",
            "missing_reason": "",
        }
        native_package = {
            "id": native_package_id,
            "problem_id": problem_id,
            "source_commit": "a" * 40,
            "revision_number": 3,
            "source_digest": "b" * 64,
            "archive_rel_path": "materializations/current.zip",
            "archive_sha256": "c" * 64,
            "archive_size_bytes": 100,
            "verification_id": "ver-current-package",
            "status": "available",
            "created_at": "2026-08-16T00:00:00Z",
            "checked_at": "2026-08-16T00:00:00Z",
            "unavailable_reason": "",
        }
        with (
            patch.object(
                runtime.problem_package_service,
                "published_readiness",
                return_value=readiness,
            ),
            patch.object(
                runtime.problem_package_service,
                "native_package",
                return_value=native_package,
            ),
            patch.object(
                runtime.export_service,
                "materialization_packages",
                return_value=[
                    {
                        "export_id": "e-current",
                        "materialization_id": native_package_id,
                        "export_type": "domjudge",
                        "filename": "sample-domjudge-v3.zip",
                    }
                ],
            ),
            patch.object(
                runtime.export_service,
                "export_archive_path",
                return_value=Path("/tmp/sample-domjudge-v3.zip"),
            ),
            patch(
                "app.impl.run_export.export.start_export_job"
            ) as start_export,
        ):
            response = export_create(
                _request(
                    "/problems/alice/sample/export/create",
                    method="POST",
                ),
                "alice/sample",
                "alice",
                format="domjudge",
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/problems/alice/sample/exports/e-current/sample-domjudge-v3.zip",
        )
        start_export.assert_not_called()

    def test_package_export_busy_returns_conflict(self) -> None:
        context = workspace_service.workspace_context(
            "alice/sample",
            "alice",
            include_recent=False,
        )
        with patch(
            "app.impl.run_export.export.start_export_job",
            return_value=False,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_create(
                    _request(
                        "/problems/alice/sample/export/create",
                        method="POST",
                    ),
                    "alice/sample",
                    "alice",
                    format="domjudge",
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(int(context["problem"]["id"]) > 0)

    def test_run_verification_details_reads_verification_record(self) -> None:
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
