from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_execute,
    db_fetch_one,
    verification_programs_for_tasks,
)
from tests.execution_result_helpers import execution_result

import asyncio
import hashlib
from html import unescape
import io
import re
import threading
import zipfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.config import CONFIG_REGISTRY
from app.service.platform.worker_queue import WorkerQueueService
from app.service.judgehost.domjudge.wire_model import DomjudgeWork
from app.service.problem.test_spec import dumps_default_tests_spec
from app.service.verification.workspace_fingerprint import verification_sources_signature
from tests.common import E2ETestBase, _wait_for_verification_workers, override_config_values
from tests.identity_helpers import canonical_test_verification_id
from tests.judgehost_support import JudgehostReply, reporting_judgehost
from tests.package_support import publish_problem, verification_builder
from tests.ui_support import (
    AUTH_COOKIE_NAME,
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
from app.impl.workspace.run_view_list import run_list_rows
from app.service.problem.readiness import ProblemReadiness, WorkspaceReadinessSubject
from app.service.execution.model import (
    CAPTURE_COMPLETE,
    CAPTURE_METADATA_ONLY,
    ExecutionPassResult,
    ExecutionUsage,
    PassArtifacts,
)
from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus
from app.service.verification.types import Kind, VerificationDetail

TEXTAREA_MAX_BYTES = int(CONFIG_REGISTRY.defaults()["TEXTAREA_MAX_BYTES"])
STATEMENT_SAMPLE_MAX_BYTES = int(
    CONFIG_REGISTRY.defaults()["STATEMENT_SAMPLE_MAX_BYTES"]
)


class TestUIRun(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = False
    seed_default_workspace = True

    def setUp(self) -> None:
        super().setUp()
        self._pending_verification_fixture_details: dict[str, VerificationDetail] = {}

    def _complete_judgehost_work(self, verification_id: str, *, output: bytes = b"7\n") -> list[str]:
        service = runtime.judgehost_task_service
        source_names: list[str] = []

        def reply(work: DomjudgeWork) -> JudgehostReply:
            files = service.domjudge_get_source_files(str(work["submitid"]))
            names = [file.filename for file in files]
            source_names.extend(names)
            wrong_answer = any(name.startswith("sanity_") or name == "wa.cpp" for name in names)
            return JudgehostReply(output=output, runresult="wrong-answer" if wrong_answer else "correct")

        try:
            with reporting_judgehost(service, reply):
                _wait_for_verification_workers(timeout_sec=15)
            record = runtime.verification_service.verification_record(verification_id)
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "ok", record)
            return source_names
        finally:
            runtime.verification_execution_service.cancel_verification(verification_id, reason="judgehost fixture finished")
            _wait_for_verification_workers(timeout_sec=5)

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
        form_data: dict[str, str | list[str]] = {
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
    ) -> ProblemReadiness:
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
        return runtime.problem_readiness_service.readiness(
            subject,
            explain_verification=True,
        )

    def _admit_verification_fixture(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int | None,
        signature: str = "",
        source_commit: str = "",
        kind: str = Kind.ALL,
        detail: VerificationDetail | None = None,
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
            self._pending_verification_fixture_details[verification_id] = detail.copy()

    def _activate_verification_fixture(
        self,
        verification_id: str,
        *,
        detail: VerificationDetail | None = None,
        tasks: list[PlannedTask],
        completions: list[TaskCompletion] | None = None,
        queued: list[tuple[str, str, str]] | None = None,
        leased: list[tuple[str, str, str]] | None = None,
    ) -> None:
        activation_detail = (
            self._pending_verification_fixture_details.pop(verification_id, {})
            if detail is None
            else detail.copy()
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
        summary: VerificationDetail | None = None,
        created_at: str = "2026-03-10T00:00:00Z",
        finished_at: str | None = "2026-03-10T00:00:01Z",
    ) -> None:
        summary_obj = summary or {}
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
        workspace = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = workspace / "tests/spec.json"
        before = spec_path.read_bytes()
        response = tests_spec_gen_script_save(
            problem="alice/sample",
            user="alice",
            gen_script_text='gen "unterminated argument',
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            str(response.headers.get("location", "")).endswith(
                "/problems/alice/sample/tests?edit=gen-script"
            )
        )
        self.assertEqual(spec_path.read_bytes(), before)

    def test_tests_spec_manual_payload_upload_and_download_routes(self) -> None:
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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

        upload_payload = UploadFile(file=io.BytesIO(b"7 8 9  \r\n10 11\t \r\n"))
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
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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
                payload_upload=UploadFile(file=io.BytesIO(oversized)),
            )
        )
        self.assertEqual(uploaded.status_code, 303)

        payload = (manual_dir / "001.in").read_text(encoding="utf-8")
        self.assertGreater(len(payload.encode("utf-8")), TEXTAREA_MAX_BYTES)
        self.assertTrue(payload.endswith("\n"))
        self.assertNotIn("\r", payload)

    def test_tests_spec_manual_payload_upload_rejects_non_utf8_payload(self) -> None:
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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
                payload_upload=UploadFile(file=io.BytesIO(b"\xff\xfe\xfd")),
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "seed\n")

    def test_tests_spec_manual_payload_upload_uses_file_size_limit_not_textarea_limit(self) -> None:
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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
                payload_upload=UploadFile(file=io.BytesIO(b"x" * 1025)),
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "seed\n")

    def test_tests_spec_add_manual_upload_route(self) -> None:
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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

        upload = UploadFile(file=io.BytesIO(b"11 22  \r\n33 44\t \r\n"))
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
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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
                manual_upload=UploadFile(file=io.BytesIO(oversized)),
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
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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
                manual_upload=UploadFile(file=io.BytesIO(b"\xff\xfe\xfd")),
            )
        )
        self.assertEqual(created.status_code, 303)
        self.assertFalse((manual_dir / "001.in").exists())

    def test_tests_spec_add_manual_upload_uses_file_size_limit_not_textarea_limit(self) -> None:
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice")
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
                manual_upload=UploadFile(file=io.BytesIO(b"x" * 1025)),
            )
        )
        self.assertEqual(created.status_code, 303)
        self.assertFalse((manual_dir / "001.in").exists())

    def test_run_http_creates_selected_tasks_and_preserves_uploaded_source(self) -> None:
        from app.main import app

        problem = f"alice/run-http-{uuid.uuid4().hex[:8]}"
        workspace = self._prepare_verification_workspace(problem)
        self._write_solution_fixture(workspace, "wa.cpp", "wrong_answer")
        test_ids = ("001", "002", "003")
        for test_id in test_ids:
            (workspace / "tests/manual" / f"{test_id}.in").write_text("7\n")
        (workspace / "tests/spec.json").write_text(json.dumps({
            "tests": [{"id": test_id, "kind": "manual"} for test_id in test_ids],
        }))
        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)
        runtime.judgehost_task_service.domjudge_register_host(f"ui-run-{self.test_id}")
        actor_id = workspace_service.known_user_id("alice")
        self.assertIsNotNone(actor_id)
        token = runtime.auth_service.create_session_for_user(actor_id)
        headers = {"cookie": f"{AUTH_COOKIE_NAME}={token}", "origin": "https://testserver"}
        upload_content = b"// exact uploaded source\r\nint main(){return 7;}\r\n"

        with TestClient(app, base_url="https://testserver") as client:
            for selected_tests, uploaded, traversal_only in (
                ([], False, False),
                (["001.in", "003.in"], True, False),
                (["001.in"], False, True),
            ):
                with self.subTest(selected_tests=selected_tests, uploaded=uploaded, traversal_only=traversal_only):
                    form = {
                        "solution_paths": ["../../escape.cpp"] if traversal_only else ["solutions/accepted.cpp", "solutions/wa.cpp", "solutions/accepted.cpp"],
                        "test_names": selected_tests,
                    }
                    response = client.post(
                        f"/problems/{problem}/run/execute",
                        data=form,
                        files={"submission_upload": ("../tmp.cpp", upload_content, "text/plain")} if uploaded else None,
                        headers=headers,
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 303)
                    location = urlparse(response.headers["location"])
                    self.assertEqual(location.path, f"/problems/{problem}/run/details")
                    verification_id = parse_qs(location.query)["verification_id"][0]
                    try:
                        activated = _wait_for_row(
                            "SELECT id FROM verification_tasks WHERE verification_id=? LIMIT 1",
                            [verification_id],
                        )
                        self.assertIsNotNone(activated, runtime.verification_service.verification_record(verification_id))
                        tasks = runtime.verification_task_store.list_rows(verification_id)
                        expected_tests = selected_tests or [f"{test_id}.in" for test_id in test_ids]
                        self.assertEqual({row["test_name"] for row in tasks}, set(expected_tests))
                        cases = [row for row in tasks if row["task_kind"] != "generate-input"]
                        expected_sources = {"solutions/accepted.cpp": "accepted"}
                        if not traversal_only:
                            expected_sources["solutions/wa.cpp"] = "wrong_answer"
                        if uploaded:
                            uploaded_sources = {row["source_path"] for row in cases if row["source_path"].startswith("uploads/")}
                            self.assertEqual(len(uploaded_sources), 1)
                            uploaded_source = uploaded_sources.pop()
                            self.assertEqual(Path(uploaded_source).name, "tmp.cpp")
                            expected_sources[uploaded_source] = "unknown"
                            ref = runtime.runtime_blob_store.ref(hashlib.sha256(upload_content).hexdigest())
                            descriptor = runtime.runtime_blob_store.descriptor(ref)
                            self.assertIsNotNone(descriptor)
                            self.assertEqual(descriptor.path.read_bytes(), upload_content)
                        self.assertEqual(
                            [(row["test_name"], row["source_path"], row["expected_behavior"]) for row in sorted(cases, key=lambda item: (item["test_name"], item["source_path"]))],
                            [(test_name, source, expected) for test_name in sorted(expected_tests) for source, expected in sorted(expected_sources.items())],
                        )
                        generators = [row for row in tasks if row["task_kind"] == "generate-input"]
                        self.assertEqual({row["test_name"] for row in generators}, set(expected_tests))
                        metadata = runtime.verification_service.verification_detail(verification_id)
                        self.assertEqual(metadata["mode"], "pass-fail")
                        self.assertEqual(metadata["selected_test_names"], expected_tests)
                    finally:
                        runtime.verification_execution_service.cancel_verification(
                            verification_id, reason="UI request coverage complete",
                        )
                        _wait_for_verification_workers(timeout_sec=5)

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

    def test_problem_reader_cannot_start_custom_run(self) -> None:
        problem = f"alice/reader-custom-{uuid.uuid4().hex[:8]}"
        self._prepare_verification_workspace(problem)
        workspace_service.ensure_user("bob")
        workspace_service.grant_repo_access(problem, "bob", "read")
        workspace_service.ensure_workspace(problem, "bob", refresh_status=False)

        with self.assertRaises(HTTPException) as raised:
            run_execute(
                problem=problem,
                user="bob",
                solution_paths=["solutions/accepted.cpp"],
                test_names=[],
                submission_upload=None,
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_admitted_verification_fails_when_layout_preparation_fails(self) -> None:
        problem = f"alice/layout-failure-{uuid.uuid4().hex[:8]}"
        user = "alice"
        self._prepare_verification_workspace(problem)
        context = workspace_service.workspace_context(
            problem,
            user,
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
        ctx = workspace_service.workspace_context(problem, "alice")
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
        ctx = workspace_service.workspace_context(problem, "alice")
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-clean-manifest-{uuid.uuid4().hex[:8]}")
        signature = verification_sources_signature(ws)
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
        ctx = workspace_service.workspace_context(problem, "alice")
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

        status = self._problem_readiness(
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_path=ws,
            dirty=False,
        )["verification"]
        self.assertEqual(status["verification_id"], verification_id)
        self.assertFalse(status["stale"])

    def test_verification_readiness_tracks_source_edits_and_restoration(self) -> None:
        problem = f"alice/verify-source-edits-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice")
        problem_id = ctx["problem"]["id"]
        workspace_id = ctx["workspace"]["id"]
        verification_id = canonical_test_verification_id(f"ver-source-edits-{uuid.uuid4().hex[:8]}")
        self._insert_stage_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=verification_sources_signature(ws),
            status="ok",
        )
        source = ws / "solutions/accepted.cpp"
        original = source.read_bytes()
        for content, stale in ((original, False), (original + b"\n// edit\n", True), (original, False)):
            source.write_bytes(content)
            for repeat in range(2):
                with self.subTest(stale=stale, repeat=repeat):
                    status = self._problem_readiness(
                        problem_id=problem_id,
                        workspace_id=workspace_id,
                        workspace_path=ws,
                    )["verification"]
                    self.assertEqual(status["verification_id"], verification_id)
                    self.assertEqual(status["stale"], stale)
                    self.assertEqual(status["result"], "ok")

    def test_rejudge_dispatches_work_even_when_case_results_are_cached(self) -> None:
        problem = f"alice/rejudge-{uuid.uuid4().hex[:8]}"
        workspace = self._prepare_verification_workspace(problem)
        self._write_solution_fixture(workspace, "wa.cpp", "wrong_answer")
        for filename in ("accepted.cpp", "wa.cpp"):
            source = workspace / "solutions" / filename
            source.write_bytes(source.read_bytes() + f"// cache fixture {problem}\n".encode())
        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)
        hostname = f"ui-rejudge-{self.test_id}"
        runtime.judgehost_task_service.domjudge_register_host(hostname)

        def start() -> str:
            response = run_execute(
                problem=problem, user="alice",
                solution_paths=["solutions/accepted.cpp", "solutions/wa.cpp"],
                test_names=[], submission_upload=None,
            )
            self.assertEqual(response.status_code, 303)
            return parse_qs(urlparse(response.headers["location"]).query)["verification_id"][0]

        first_id = start()
        first_sources = self._complete_judgehost_work(first_id)
        self.assertIn("accepted.cpp", first_sources)
        self.assertIn("wa.cpp", first_sources)
        cached_id = start()
        cached_sources = self._complete_judgehost_work(cached_id)
        self.assertNotIn("accepted.cpp", cached_sources)
        self.assertNotIn("wa.cpp", cached_sources)

        response = run_rejudge(problem, "alice", verification_id=cached_id)
        self.assertEqual(response.status_code, 303)
        rejudge_id = parse_qs(urlparse(response.headers["location"]).query)["verification_id"][0]
        self.assertNotIn(rejudge_id, {first_id, cached_id})
        rejudge_sources = self._complete_judgehost_work(rejudge_id)
        self.assertIn("accepted.cpp", rejudge_sources)
        self.assertIn("wa.cpp", rejudge_sources)
        tasks = runtime.verification_task_store.list_rows(rejudge_id)
        self.assertEqual(
            {(row["source_path"], row["expected_behavior"]) for row in tasks if row["task_kind"] != "generate-input"},
            {("solutions/accepted.cpp", "accepted"), ("solutions/wa.cpp", "wrong_answer")},
        )

    def test_published_verification_can_be_rejudged_but_not_cancelled(self) -> None:
        ws = self._prepare_verification_workspace("alice/sample")
        self._write_solution_fixture(ws, "fixture.cpp", "unknown")
        published = revision_commit(problem="alice/sample", user="alice", message="publish rejudge fixture")
        self.assertEqual(published.status_code, 303)
        workspace_service.ensure_user("bob")
        workspace_service.grant_repo_access("alice/sample", "bob", "read")
        workspace_service.ensure_workspace("alice/sample", "bob", refresh_status=False)
        ctx = workspace_service.workspace_context("alice/sample", "alice")
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

        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)
        runtime.judgehost_task_service.domjudge_register_host(f"ui-published-rejudge-{self.test_id}")
        rejudge_response = run_rejudge("alice/sample", "bob", verification_id=published_id)
        self.assertEqual(rejudge_response.status_code, 303)
        rejudge_id = parse_qs(urlparse(rejudge_response.headers["location"]).query)["verification_id"][0]
        self.assertNotEqual(rejudge_id, published_id)
        try:
            activated = _wait_for_row(
                "SELECT id FROM verification_tasks WHERE verification_id=? LIMIT 1", [rejudge_id],
            )
            self.assertIsNotNone(activated, runtime.verification_service.verification_record(rejudge_id))
            bob_context = workspace_service.workspace_context("alice/sample", "bob")
            record = runtime.verification_service.verification_record(rejudge_id)
            self.assertEqual(record["workspace_id"], bob_context["workspace"]["id"])
        finally:
            runtime.verification_execution_service.cancel_verification(rejudge_id, reason="published rejudge coverage complete")
            _wait_for_verification_workers(timeout_sec=5)
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
        alice_ctx = workspace_service.workspace_context("alice/sample", "alice")
        bob_ctx = workspace_service.workspace_context("alice/sample", "bob")
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

    def test_verification_list_includes_package_runs(self) -> None:
        ctx = workspace_service.workspace_context(
            "alice/sample",
            "alice",
        )
        package_verification_id = canonical_test_verification_id(
            f"ver-package-visible-{uuid.uuid4().hex[:8]}"
        )
        self._insert_stage_verification(
            verification_id=package_verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=None,
            kind=Kind.PACKAGE,
            source_commit=str(ctx["workspace"]["head_commit"] or ""),
        )

        rows = run_list_rows(
            int(ctx["problem"]["id"]),
            int(ctx["workspace"]["id"]),
            Path(str(ctx["workspace"]["path"])),
            actor_user_id=int(ctx["user"]["id"]),
        )

        package_row = next(
            row for row in rows if row["id"] == package_verification_id
        )
        self.assertEqual(package_row["kind"], Kind.PACKAGE.value)

    def test_run_cancel_marks_running_verification_cancelled(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice")
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-cancel-{uuid.uuid4().hex[:8]}")
        run_id = f"r-cancel-running-{uuid.uuid4().hex[:8]}"
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            detail={"mode": "pass-fail", "source_paths": ["solutions/accepted.cpp"]},
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
        ctx = workspace_service.workspace_context("alice/sample", "alice")
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-cancel-pending-{uuid.uuid4().hex[:8]}")
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            detail={"mode": "pass-fail", "source_paths": ["solutions/accepted.cpp"]},
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
        ctx = workspace_service.workspace_context("alice/sample", "alice")
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"inv-cancel-domjudge-pending-{uuid.uuid4().hex[:8]}")
        run_id = f"r-cancel-domjudge-pending-{uuid.uuid4().hex[:8]}"
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            detail={"mode": "pass-fail", "source_paths": ["solutions/accepted.cpp"]},
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


    def test_verification_waits_for_worker_capacity_and_rejection_is_durable(self) -> None:
        problem = f"alice/verify-queued-{uuid.uuid4().hex[:8]}"
        self._prepare_verification_workspace(problem)
        context = workspace_service.workspace_context(problem, "alice")
        rejected_problem = f"alice/verify-rejected-{uuid.uuid4().hex[:8]}"
        rejected_workspace = self._prepare_verification_workspace(rejected_problem)
        rejected_context = workspace_service.workspace_context(rejected_problem, "alice")
        rejected_id = canonical_test_verification_id(f"queue-rejection-{uuid.uuid4().hex}")
        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)
        runtime.judgehost_task_service.domjudge_register_host(f"ui-queue-{self.test_id}")
        started = threading.Event()
        release = threading.Event()
        queue = WorkerQueueService(worker_count=1, queue_capacity=1)
        queued_id = ""

        def hold_worker() -> None:
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release worker")

        with patch.object(runtime, "worker_queue_service", queue):
            try:
                blocker, accepted, reason = queue.submit(name="capacity blocker", fn=hold_worker)
                self.assertTrue(accepted, reason)
                self.assertTrue(started.wait(timeout=2))
                response = verification_start(problem=problem, user="alice", page="run")
                self.assertEqual(response.status_code, 303)
                queued = runtime.verification_service.list_visible_verification_rows(
                    context["problem"]["id"], context["workspace"]["id"], limit=1,
                )
                self.assertEqual(len(queued), 1)
                queued_id = queued[0]["id"]
                self.assertEqual(queued[0]["status"], "queued")
                self.assertEqual(runtime.verification_task_store.list_rows(queued_id), [])

                with self.assertRaisesRegex(RuntimeError, "queue rejected"):
                    workspace_context_job.start_verification_job(
                        runtime,
                        rejected_problem,
                        "alice",
                        problem_id=rejected_context["problem"]["id"],
                        workspace_id=rejected_context["workspace"]["id"],
                        workspace_head=rejected_context["workspace"]["head_commit"],
                        workspace_dirty=bool(rejected_context["workspace"]["dirty"]),
                        targets=[],
                        verification_id=rejected_id,
                        allow_package_certification=True,
                        workspace_path=rejected_workspace,
                    )
                rejected = db_fetch_one(
                    "SELECT status,fail_reason,finished_at FROM verifications WHERE id=?",
                    [rejected_id],
                )
                self.assertIsNotNone(rejected)
                self.assertEqual(rejected["status"], "failed")
                self.assertIn("queue rejected", rejected["fail_reason"])
                self.assertTrue(rejected["finished_at"])

                release.set()
                blocker.join(timeout=2)
                self.assertIsNone(blocker.exception())
                activated = _wait_for_row(
                    "SELECT id FROM verification_tasks WHERE verification_id=? LIMIT 1",
                    [queued_id],
                )
                self.assertIsNotNone(activated, runtime.verification_service.verification_record(queued_id))
            finally:
                release.set()
                if queued_id:
                    runtime.verification_execution_service.cancel_verification(queued_id, reason="queue test complete")
                _wait_for_verification_workers(timeout_sec=5)
                queue.stop()

    def test_reader_verification_is_complete_but_only_writer_certifies_package(self) -> None:
        problem = f"alice/certification-{uuid.uuid4().hex[:8]}"
        workspace = self._prepare_verification_workspace(problem)
        (workspace / "tests/spec.json").write_text(json.dumps({
            "tests": [{"id": "001", "kind": "manual", "sample": True}],
        }), encoding="utf-8")
        published = revision_commit(problem=problem, user="alice", message="publish certification fixture")
        self.assertEqual(published.status_code, 303)
        alice_context = workspace_service.workspace_context(problem, "alice")
        problem_id = alice_context["problem"]["id"]
        revision = runtime.problem_package_service.published_revision(problem_id)
        package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(problem_id, input_bytes=b"7\n", answer_bytes=b"7\n", verification_kind="package"),
        )
        self.assertFalse(runtime.problem_package_service.native_package_verified(package))
        package_verification_id = package["verification_id"]
        workspace_service.ensure_user("bob")
        workspace_service.grant_repo_access(problem, "bob", "read")
        workspace_service.ensure_workspace(problem, "bob", refresh_status=False)
        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)
        hostname = f"ui-certification-{self.test_id}"
        runtime.judgehost_task_service.domjudge_register_host(hostname)

        for user, can_certify in (("bob", False), ("alice", True)):
            with self.subTest(user=user):
                response = verification_start(problem=problem, user=user, page="run")
                self.assertEqual(response.status_code, 303)
                context = workspace_service.workspace_context(problem, user)
                rows = runtime.verification_service.list_visible_verification_rows(
                    problem_id, context["workspace"]["id"], limit=1,
                )
                self.assertEqual(len(rows), 1)
                verification_id = rows[0]["id"]
                self.assertEqual(rows[0]["workspace_id"], context["workspace"]["id"])
                self._complete_judgehost_work(verification_id)
                current = runtime.problem_package_service.native_package(package["id"])
                self.assertIsNotNone(current)
                self.assertEqual(runtime.problem_package_service.native_package_verified(current), can_certify)
                self.assertEqual(current["verification_id"], verification_id if can_certify else package_verification_id)

    def test_pass_fail_sample_json_exposes_every_pass(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice")
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

        def store(payload: bytes) -> str:
            return (runtime.runtime_blob_store.put_bytes(payload).blob_ref or "")

        original_input_ref = store(b"original input\n")
        answer_ref = store(b"canonical answer\n")
        first_input_ref = store(b"original input\n")
        second_input_ref = store(b"next pass input\n")
        first_output_ref = store(b"first pass output\n")
        second_output_ref = store(b"second pass output\n")
        common_ref = store(b"metadata\n")
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
        ctx = workspace_service.workspace_context("alice/sample", "alice")
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

        def store(payload: bytes) -> str:
            return (runtime.runtime_blob_store.put_bytes(payload).blob_ref or "")

        input_ref = store(b"testcase seed\n")
        answer_ref = store(b"accepted\n")
        input_one_ref = store(b"first pass input\n")
        input_two_ref = store(b"second pass input\n")
        common_ref = store(b"metadata\n")
        jury_one_ref = store(b"first pass accepted\n")
        jury_two_ref = store(b"second pass accepted\n")
        transcript_one = (
            b"[  0.019s/5]>: ping\n\n"
            b"[  0.024s/4]<: pong\n"
            b"[  0.025s/0]]"
            + b"[  0.026s/1]>: x\n" * 998
        )
        transcript_two = b"[  0.031s/5]>: final\n" + b"broken"
        transcript_one_ref = store(transcript_one)
        transcript_two_ref = store(transcript_two)
        other_transcript_ref = store(b"[  0.001s/4]>: trap\n")
        other_jury_ref = store(b"must not be read\n")
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

        alice_ctx = workspace_service.workspace_context("alice/sample", "alice")
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(f"ver-collab-detail-{uuid.uuid4().hex[:8]}")
        self._admit_verification_fixture(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=alice_workspace_id,
            detail={
                "mode": "pass-fail",
                "source_paths": ["solutions/std.cpp"],
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
        output_ref = (runtime.runtime_blob_store.put_bytes(b"6\n").blob_ref or "")
        input_ref = (runtime.runtime_blob_store.put_bytes(b"1 2 3\n").blob_ref or "")
        answer_ref = (runtime.runtime_blob_store.put_bytes(b"6\n").blob_ref or "")
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
        from app.main import app
        from fastapi.testclient import TestClient
        from tests.ui_support import AUTH_COOKIE_NAME

        contest_slug = f"history-scope-{uuid.uuid4().hex[:8]}"
        actor_id = int(alice_ctx["user"]["id"])
        contest_id = runtime.contest_service.create_contest_with_owner(
            slug=contest_slug, title="History scope", owner_user_id=actor_id,
        )
        runtime.contest_service.add_problem(contest_id, "A", problem_id, actor_id)
        token = runtime.auth_service.create_session_for_user(actor_id)
        client = TestClient(app)
        self.addCleanup(client.close)
        scoped = client.get(
            f"/problems/alice/sample/run/details/test-fragment?verification_id={verification_id}&test=001.in&program_id=solution-0&contest={contest_slug}",
            headers={"cookie": f"{AUTH_COOKIE_NAME}={token}"},
        )
        self.assertEqual(scoped.status_code, 200)
        links = [unescape(value) for value in re.findall(r'href="([^"]+)"', scoped.text)]
        link = next(value for value in links if "/run/details/sample-json?" in value)
        self.assertEqual(parse_qs(urlparse(link).query)["contest"], [contest_slug])
        sample_response = client.get(link, headers={"cookie": f"{AUTH_COOKIE_NAME}={token}"})
        self.assertEqual(sample_response.status_code, 200, sample_response.text)
        self.assertEqual(sample_response.json()["passes"][0]["output"], "6\n")
        workspace_service.grant_repo_access("alice/sample", "bob", "read")
        bob_ctx = workspace_service.workspace_context("alice/sample", "bob")
        config = Path(bob_ctx["workspace"]["path"]) / "config/problem.json"
        original = config.read_bytes()
        before = dict(db_fetch_one("SELECT * FROM workspaces WHERE id=?", [bob_ctx["workspace"]["id"]]))
        try:
            config.write_text("{broken config", encoding="utf-8")
            history = run_details_test_fragment(
                _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in&program_id=solution-0"),
                "alice/sample", "bob",
            )
            self.assertEqual(history.status_code, 200)
            self.assertIn("std.cpp", history.body.decode())
            after = dict(db_fetch_one("SELECT * FROM workspaces WHERE id=?", [bob_ctx["workspace"]["id"]]))
            self.assertEqual(after, before)
        finally:
            config.write_bytes(original)
        workspace_service.ensure_user("charlie")
        with self.assertRaises(HTTPException) as denied:
            run_details_test_fragment(
                _request("/problems/alice/sample/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
                "alice/sample", "charlie",
            )
        self.assertIn(denied.exception.status_code, {403, 404})

    def test_package_create_redirects_to_a_retrievable_published_archive(self) -> None:
        from app.main import app

        problem = f"alice/package-download-{uuid.uuid4().hex[:8]}"
        workspace = self._prepare_verification_workspace(problem)
        _workspace, problem_id, _commit = publish_problem(workspace, problem, "alice")
        revision = runtime.problem_package_service.published_revision(problem_id)
        package = runtime.problem_package_service.ensure_native_package(
            revision, verification_builder(problem_id),
        )
        actor_id = workspace_service.known_user_id("alice")
        token = runtime.auth_service.create_session_for_user(actor_id)
        headers = {"cookie": f"{AUTH_COOKIE_NAME}={token}", "origin": "https://testserver"}
        with TestClient(app, base_url="https://testserver") as client:
            for form in ({"format": "native"}, {"format": "domjudge", "create_native": "1"}):
                with self.subTest(form=form):
                    response = client.post(
                        f"/problems/{problem}/export/create", data=form,
                        headers=headers, follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 303, response.text)
                    self.assertEqual(response.headers["location"], f"/problems/{problem}/native-packages/{package['id']}/download")
                    archive_response = client.get(response.headers["location"], headers=headers)
                    self.assertEqual(archive_response.status_code, 200)
                    with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
                        self.assertEqual(archive.read("solutions/accepted.cpp"), (workspace / "solutions/accepted.cpp").read_bytes())
            self.assertEqual(runtime.export_service.problem_export_jobs(problem_id, limit=1), [])
