from __future__ import annotations

from .db_helpers import (
    db_execute,
    db_fetch_one,
    judgehost_cases_for_run,
    judgehost_fetch_case,
    judgehost_fetch_batch,
)

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

from app.impl.workspace.verification_dag import _prepared_payload_for_uploaded_source
from app.service.verification.plan import VerificationTestPlan
from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.batch_scheduler_models import CompileSubmission, ExecutionBatchSpec
from app.service.judgehost.identity import domjudge_job_id, domjudge_submit_id
from app.service.judgehost.api import Judgehost
from app.service.judgehost.runtime import domjudge_rewrite_untrusted_runresult
from app.service.platform.hashing import domjudge_executable_hash
from app.service.verification.task_scheduler import (
    VerificationRuntimeCallbacks,
    VerificationRuntimeCoordinator,
    register_verification_runtime_coordinator,
    unregister_verification_runtime_coordinator,
)
from app.service.verification.task_store import VerificationTaskStore
from .common import E2ETestBase, config


def _canonical_verification_id(label: str) -> str:
    return f"ver-{hashlib.sha256(label.encode('utf-8')).hexdigest()}"


class TestJudgehostService(E2ETestBase):
    seed_default_workspace = True

    @staticmethod
    def _compile_submission(source_name: str, compile_key: str) -> CompileSubmission:
        return CompileSubmission(
            compile_key=compile_key,
            submit_id=domjudge_submit_id(compile_key),
            source_name=source_name,
            source_bytes=b"int main() { return 0; }\n",
            extra_source_items=(),
            compile_files=(),
        )

    @staticmethod
    def _work_rows_for_task(
        service: Judgehost,
        rows: list[dict[str, object]],
        task_id: str,
    ) -> list[dict[str, object]]:
        return [
            row
            for row in rows
            if (
                (case := judgehost_fetch_case(service, int(row["judgetaskid"]))) is not None
                and str(case["task_id"]) == task_id
            )
        ]

    def _fresh_judgehost_service(self) -> Judgehost:
        service = Judgehost(
            config.db,
            config.workspace_service,
            config.fs_manager,
            config.settings,
            config.constants,
            verification_task_store=config.verification_task_store,
            judge_fs_index_service=config.judge_fs_index_service,
        )
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True
        return service

    def _verification_run_row(self, run_id: str, verification_id: str = "") -> dict[str, object] | None:
        safe_run_id = str(run_id or "").strip()
        if not safe_run_id:
            return None
        safe_verification_id = str(verification_id or "").strip()
        service = config.judgehost_task_service
        task_row = service.state.task_registry.get_for_run(safe_run_id)
        if task_row is not None and safe_verification_id:
            if str(task_row.get("verification_id") or "") != safe_verification_id:
                task_row = None
        if task_row is not None:
            row_verification_id = str(task_row.get("verification_id") or "")
            summary = service.queue.load_run_summary(safe_run_id, row_verification_id)
            return {
                "status": str(task_row.get("run_status") or "").strip(),
                "summary": dict(summary),
                "verification_id": row_verification_id,
            }
        candidates = [safe_verification_id] if safe_verification_id else [f"ver-{safe_run_id}"]
        task_store = config.verification_task_store
        for candidate in candidates:
            token = str(candidate or "").strip()
            if not token:
                continue
            rows = task_store.list_rows(token)
            matched_rows = [row for row in rows if str(row["logical_run_id"] or "") == safe_run_id]
            if not matched_rows:
                continue
            tests = []
            for row in matched_rows:
                verdict = str(row["verdict"] or "")
                tests.append(
                    {
                        "test": str(row["test_name"] or ""),
                        "verdict": verdict,
                        "time_ms": int(round(float(row["runtime_sec"] or 0.0) * 1000.0)),
                        "memory_kb": int(row["memory_kb"] or 0),
                        "message": str(row["feedback_text"] or row["error_text"] or ""),
                        "output_ref": str(row["output_ref"] or ""),
                        "feedback_files": [],
                        "passes": [
                            {
                                "index": 1,
                                "verdict": verdict,
                                "feedback": str(row["feedback_text"] or row["error_text"] or ""),
                                "output_ref": str(row["output_ref"] or ""),
                            }
                        ],
                    }
                )
            statuses = {str(row["status"] or "") for row in matched_rows}
            if statuses == {VerificationTaskStore.TASK_DONE}:
                run_status = "ok"
            elif VerificationTaskStore.TASK_FAILED in statuses:
                run_status = "failed"
            elif VerificationTaskStore.TASK_CANCELLED in statuses:
                run_status = "cancelled"
            else:
                run_status = "running"
            return {
                "status": run_status,
                "summary": {
                    "source": str(matched_rows[0]["source_path"] or ""),
                    "status": run_status,
                    "tests": tests,
                    "error": str(matched_rows[0]["error_text"] or ""),
                },
                "verification_id": token,
            }
        return None

    def _verification_artifact_root(self, verification_id: str) -> Path:
        artifact_path = config.verification_service.artifact_path_for_verification(str(verification_id or "").strip())
        if not artifact_path:
            raise AssertionError(f"missing artifact_path for verification: {verification_id}")
        return Path(artifact_path).resolve()

    def test_domjudge_add_judging_run_finalizes_matching_verification_task_immediately(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = f"ver-{uuid.uuid4().hex}"
        verification_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        verification_root.mkdir(parents=True, exist_ok=True)
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
            status="running",
            detail={"verification_id": verification_id, "task_graph": True, "status": "running"},
        )

        run_id = f"r-immediate-finalize-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(
            verification_id,
            [("001.in", "ok\n", "ok\n"), ("002.in", "ok2\n", "ok2\n")],
        )
        judgehost_task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in", "002.in"],
            verification_id=verification_id,
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
            task_kind="solution-run",
            persist_verification_run=False,
        )
        task_store = config.verification_task_store
        task_store.replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-case-001",
                    "task_kind": "solution-run",
                    "source_path": "solutions/ac.cpp",
                    "logical_run_id": run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": "vt-case-002",
                    "task_kind": "solution-run",
                    "source_path": "solutions/ac.cpp",
                    "logical_run_id": run_id,
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        task_store.set_task_queued("vt-case-001", run_id=run_id, judgehost_task_id=judgehost_task_id)
        task_store.set_task_queued("vt-case-002", run_id=run_id, judgehost_task_id=judgehost_task_id)

        callbacks = VerificationRuntimeCallbacks(
            publish_task=lambda _row: (_ for _ in ()).throw(RuntimeError("unexpected publish")),
            probe_task_case_cache=lambda _task_ids, _limit: set(),
            resolve_case_result=lambda queued_task_id, test_name: service.poll_task_case_result(queued_task_id, test_name),
            cancel_queued_tasks=lambda _reason: None,
        )
        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=task_store,
            callbacks=callbacks,
            edges=[],
        )
        register_verification_runtime_coordinator(verification_id, coordinator)
        coordinator_thread = threading.Thread(target=coordinator.run, daemon=True)
        coordinator_thread.start()
        try:
            service.domjudge_register_host("judgehost-immediate-finalize")
            leased = service.domjudge_fetch_work("judgehost-immediate-finalize", max_batchsize=1)
            self.assertEqual(len(leased), 1)
            case_id = int(leased[0].get("judgetaskid") or 0)
            self.assertGreater(case_id, 0)
            task_store.set_task_leased("vt-case-001")

            metadata = b"cpu-time: 0.001\nwall-time: 0.001\nmemory-bytes: 4096\n"
            service.domjudge_update_judging(
                "judgehost-immediate-finalize",
                case_id,
                {
                    "compile_success": "1",
                    "output_compile": "",
                    "compile_metadata": "",
                },
            )
            with patch.object(service.result, "_domjudge_finalize_batch_if_ready", return_value=None):
                service.domjudge_add_judging_run(
                    "judgehost-immediate-finalize",
                    case_id,
                    {
                        "runresult": "correct",
                        "runtime": "0.001",
                        "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                        "output_diff": "",
                        "output_error": "",
                        "output_system": "",
                        "metadata": base64.b64encode(metadata).decode("ascii"),
                        "compare_metadata": "",
                    },
                )

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
                if str(rows["vt-case-001"]["status"] or "") == VerificationTaskStore.TASK_DONE:
                    break
                time.sleep(0.01)
            rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
            self.assertEqual(str(rows["vt-case-001"]["status"] or ""), VerificationTaskStore.TASK_DONE)
            self.assertEqual(str(rows["vt-case-002"]["status"] or ""), VerificationTaskStore.TASK_QUEUED)
        finally:
            unregister_verification_runtime_coordinator(verification_id)
            coordinator.enqueue_cancel("test shutdown")
            coordinator_thread.join(timeout=2.0)
            self.assertFalse(coordinator_thread.is_alive())

    def _seed_verification_test_artifacts(self, verification_id: str, items: list[tuple[str, str, str]]) -> None:
        for test_name, input_text, answer_text in items:
            input_ref = config.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name=test_name,
                role="input",
                file_name=test_name,
                payload=input_text.encode("utf-8"),
            )
            answer_name = f"{Path(test_name).stem}.ans"
            answer_ref = config.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name=test_name,
                role="answer",
                file_name=answer_name,
                payload=answer_text.encode("utf-8"),
            )
            config.verification_service.update_verification_artifact_refs(
                verification_id,
                test_name,
                {"input_ref": input_ref, "answer_ref": answer_ref},
            )
        metadata = config.verification_service.verification_detail(verification_id)
        metadata["selected_test_names"] = [test_name for test_name, _input, _answer in items]
        metadata["run_config_json"] = json.dumps({"checker_mode": "testlib", "checker_args": [], "pass_limit": 1})
        config.verification_service.persist_verification_detail(verification_id, metadata)

    def _seed_build_verification(self, verification_id: str) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "ac.cpp").write_text(
            "#include <bits/stdc++.h>\n"
            "using namespace std;\n"
            "int main(){string s; if(cin>>s) cout<<s<<\"\\n\"; return 0;}\n",
            encoding="utf-8",
        )
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        artifact_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            kind="all",
            status="ok",
            detail={},
        )
        self._seed_verification_test_artifacts(verification_id, [("001.in", "ok\n", "ok\n")])
        db_execute(
            "UPDATE verifications SET created_at=?, finished_at=? WHERE id=?",
            ["2026-02-28T00:00:00Z", "2026-02-28T00:00:00Z", verification_id],
        )

    def _judge_index_entry_count(self, kind: str) -> int:
        return int(config.judge_fs_index_service.count_entries(kind=kind))

    @staticmethod
    def _reset_task_queue_state(service) -> None:
        service.reset_runtime_state()

    def _lease_only_case(self, service, batch_id: int, hostname: str) -> int:
        rows = service.state.batch_scheduler.lease_cases(
            batch_id,
            hostname=hostname,
            limit=1,
            now_text="2026-04-14T00:00:00+00:00",
        )
        self.assertEqual(len(rows), 1)
        return int(rows[0]["id"])

    def _commit_case_result(
        self,
        service,
        *,
        case_id: int,
        hostname: str,
        test_name: str,
        runresult: str,
        verdict: str,
        feedback_text: str = "",
        feedback_files: list[str] | None = None,
    ) -> None:
        claim = service.state.batch_scheduler.claim_case_reporting(
            case_id,
            hostname=hostname,
            now_text="2026-04-14T00:00:01+00:00",
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        result = build_case_result(
            test_name=test_name,
            runresult=runresult,
            verdict=verdict,
            runtime_sec=0.012,
            cpu_sec=0.011,
            wall_sec=0.025,
            memory_kb=1404,
            score_text="",
            output_run_rel="",
            output_error_rel="",
            output_system_rel="",
            output_diff_rel="",
            metadata_rel="",
            compare_metadata_rel="",
            team_message_rel="",
            feedback_text=feedback_text,
            feedback_files=[] if feedback_files is None else feedback_files,
            answer_correct=False,
        )
        self.assertEqual(
            service.state.batch_scheduler.commit_case_result(
                case_id,
                generation=claim.generation,
                result=result,
                updated_at="2026-04-14T00:00:01+00:00",
            ),
            "reported",
        )

    @staticmethod
    def _insert_task_receipt(
        service,
        *,
        task_id: str,
        run_id: str,
        verification_id: str,
        task_kind: str = "main-correct",
    ) -> None:
        service.state.task_registry.insert(
            {
                "id": task_id,
                "run_id": run_id,
                "problem_slug": "owner/problem",
                "username": "owner",
                "artifact_verification_id": verification_id,
                "verification_id": verification_id,
                "mode": "pass-fail",
                "status": service.STATUS_LEASED,
                "payload": {
                    "task_kind": task_kind,
                    "verification_source": "run.execute",
                    "source_path": "solutions/std.cpp",
                },
                "result": {},
                "persist_verification_run": False,
                "error_text": "",
                "created_at": "2026-04-14T00:00:00+00:00",
                "updated_at": "2026-04-14T00:00:00+00:00",
                "completed_at": "",
                "summary": {
                    "mode": "pass-fail",
                    "source": "solutions/std.cpp",
                    "tests": [],
                    "compile_diagnostics": [],
                    "error": "",
                },
                "enqueue_fingerprint": "",
            }
        )

    def test_domjudge_executable_hash_uses_md5_contract(self) -> None:
        files: list[tuple[str, bytes, bool]] = [
            ("z-note.txt", b"", False),
            ("run", b"#!/bin/sh\necho ok\n", True),
            ("main.cpp", b"int main(){return 0;}\n", False),
        ]
        got = domjudge_executable_hash(files)
        rows = sorted(files, key=lambda item: str(item[0]))
        parts: list[str] = []
        for filename, content, is_exec in rows:
            content_md5 = hashlib.md5(bytes(content)).hexdigest()
            parts.append(f"{content_md5}{filename}{'1' if is_exec else ''}")
        expected = hashlib.md5("".join(parts).encode("utf-8")).hexdigest()
        self.assertEqual(got, expected)
        self.assertRegex(got, r"^[0-9a-f]{32}$")

    def test_domjudge_state_store_seeds_ids_above_process_base(self) -> None:
        store = BatchScheduler(id_base=123456)
        batch_id = store.create_batch_with_cases(
            task_id=f"jt-id-base-{uuid.uuid4().hex[:8]}",
            run_id=f"r-id-base-{uuid.uuid4().hex[:8]}",
            execution_signature="a" * 64,
            verification_id="ver-1",
            compile_key="8" * 64,
            compile_submission=self._compile_submission("main.cpp", "8" * 64),
            contest_id="local",
            mode="pass-fail",
            source_name="main.cpp",
            source_path="/tmp/main.cpp",
            work_root="/tmp/work",
            compile_hash="1" * 32,
            run_hash="2" * 32,
            compare_hash="3" * 32,
            source_hash="4" * 64,
            compile_config_json="{}",
            run_config_json="{}",
            compare_config_json="{}",
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=0,
            service_class="background",
            batch_spec=ExecutionBatchSpec(),
            created_at="2026-05-08T00:00:00+00:00",
            case_rows=[
                {
                    "task_id": "jt-id-base-case",
                    "run_id": "r-id-base-case",
                    "test_name": "001.in",
                    "ordinal": 1,
                    "testcase_id": 1,
                    "testcase_hash": "5" * 64,
                    "testcase_input_hash": "6" * 64,
                    "testcase_answer_hash": "7" * 64,
                    "input_ref": "cache://case/input",
                    "answer_ref": "cache://case/answer",
                    "status": "pending",
                }
            ],
        )
        self.assertGreater(batch_id, 123456)
        cases = store.cases_for_batch(batch_id)
        self.assertEqual(len(cases), 1)
        self.assertGreater(int(cases[0]["id"]), 123456)

    def test_set_host_enabled_preserves_host_status_shape(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        old_enabled = bool(service.state.enabled)
        old_token = str(service.state.api_token or "")
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        service.state.enabled = True
        service.state.api_token = "host-shape-token"

        service.domjudge_register_host("judgehost-shape-check")
        before = service.status()
        before_hosts = before.get("hosts") if isinstance(before, dict) else []
        before_host = next(
            item
            for item in before_hosts
            if str(item.get("hostname") or "") == "judgehost-shape-check"
        )
        self.assertEqual(before_host.get("judged_case_count"), 0)
        self.assertIsNone(before_host.get("last_judging_at"))
        self.assertIsNone(before_host.get("recent_avg_per_case_sec"))
        self.assertNotIn("load_5m", before_host)

        release = service.set_host_enabled("judgehost-shape-check", False)
        self.assertIsInstance(release, dict)

        after = service.status()
        after_hosts = after.get("hosts") if isinstance(after, dict) else []
        host = next((item for item in after_hosts if str(item.get("hostname") or "") == "judgehost-shape-check"), None)
        self.assertIsNotNone(host)
        self.assertFalse(bool(host.get("enabled")))

    def test_domjudge_work_root_uses_transient_cache_root(self) -> None:
        service = config.judgehost_task_service
        work_root = service.toolkit.work_root("jt-cache-root-check").resolve()
        expected_root = config.fs_manager.judgehost_runs_root.resolve()
        self.assertEqual(work_root.parent, expected_root)
        self.assertTrue(str(work_root).startswith(str(expected_root)))

    def test_wait_for_task_result_keeps_transient_runs_out_of_durable_artifact_paths(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        verification_id = f"ver-{uuid.uuid4().hex}"
        run_id = f"r-jh-transient-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=verification_id,
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="verification.start",
            persist_verification_run=False,
        )
        service.state.task_registry.transition(
            task_id,
            expected={service.STATUS_QUEUED},
            status=service.STATUS_REPORTING,
        )
        service.state.task_registry.transition(
            task_id,
            expected={service.STATUS_REPORTING},
            status=service.STATUS_COMPLETED,
            updates={
                "run_status": "ok",
                "error_text": "",
                "summary": {
                "mode": "pass-fail",
                "source": "solutions/ac.cpp",
                "tests": [],
                "status": "ok",
                },
            },
        )
        result = service.wait_for_task_result(task_id, timeout_sec=1.0)
        self.assertEqual(str(result.get("artifact_path") or ""), "")
        self.assertFalse((self._verification_artifact_root(verification_id) / "runs" / run_id).exists())

    def test_judgehost_run_submission_uses_queue_service(self) -> None:
        service = config.judgehost_task_service
        with patch.object(service, "enabled", return_value=True), patch.object(
            service, "auth_token_configured", return_value=True
        ), patch.object(service, "enqueue_task", return_value="jt-x") as mocked_enqueue, patch.object(
            service, "wait_for_task", return_value="r-x"
        ) as mocked_wait:
            run_id = service.run_submission(
                problem="alice/sample",
                username="alice",
                artifact_verification_id="b-x",
                submission_path="solutions/ac.cpp",
                run_id="r-x",
            )
        self.assertEqual(run_id, "r-x")
        self.assertEqual(mocked_enqueue.call_count, 1)
        self.assertEqual(mocked_wait.call_count, 1)

    def test_domjudge_fetch_uses_two_case_default_and_honors_explicit_one(self) -> None:
        service = self._fresh_judgehost_service()
        self.addCleanup(service.reset_runtime_state)
        service.state.fetch_batch_size = 2
        verification_id = f"b-jh-default-batch-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-default-batch-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(
            verification_id,
            [
                ("001.in", "one\n", "one\n"),
                ("002.in", "two\n", "two\n"),
                ("003.in", "three\n", "three\n"),
                ("004.in", "four\n", "four\n"),
            ],
        )
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in", "002.in", "003.in", "004.in"],
            verification_id=_canonical_verification_id(f"inv-{verification_id}"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        host = "judgehost-default-batch"
        service.domjudge_register_host(host)

        leader = service.domjudge_fetch_work(host)
        self.assertEqual(len(leader), 1)
        service.domjudge_update_judging(
            host,
            int(leader[0]["judgetaskid"]),
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        self.assertEqual(len(service.domjudge_fetch_work(host, max_batchsize=1)), 1)
        self.assertEqual(len(service.domjudge_fetch_work(host)), 2)

    def test_domjudge_endpoints_finalize_run(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-dom-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertTrue(task_id.startswith("jt-"))

        register_rows = service.domjudge_register_host("judgehost-official")
        self.assertEqual(register_rows, [])

        tasks = service.domjudge_fetch_work("judgehost-official", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        row = tasks[0]
        self.assertEqual(str(row.get("type") or ""), "judging_run")
        judgetask_id = int(row.get("judgetaskid") or 0)
        contest_id = str(row.get("contestid") or "")
        submit_id = str(row.get("submitid") or "")
        compile_script_id = int(row.get("compile_script_id") or 0)
        testcase_id = int(row.get("testcase_id") or 0)
        self.assertGreater(judgetask_id, 0)
        self.assertGreater(compile_script_id, 0)
        self.assertGreater(testcase_id, 0)

        source_files = service.domjudge_get_source_files(submit_id, contest_id=contest_id)
        self.assertTrue(source_files)
        self.assertEqual(str(source_files[0].get("filename") or ""), "ac.cpp")

        compile_files = service.domjudge_get_executable_files("compile", compile_script_id)
        self.assertTrue(any(str(item.get("filename") or "") == "run" for item in compile_files))
        compile_run = next((item for item in compile_files if str(item.get("filename") or "") == "run"), {})
        compile_run_text = base64.b64decode(str(compile_run.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn('exec g++ -x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE -I. "$MAIN" -o "$DEST"', compile_run_text)

        self.assertEqual(testcase_id, judgetask_id)
        testcase_files = service.domjudge_get_testcase_files(testcase_id, hostname="judgehost-official")
        self.assertEqual(len(testcase_files), 2)
        self.assertEqual({str(item.get("filename") or "") for item in testcase_files}, {"input", "output"})
        case_row = judgehost_fetch_case(service, judgetask_id)
        self.assertIsNotNone(case_row)
        self.assertTrue(str(case_row["input_ref"] or "").startswith("cache://"))
        self.assertTrue(str(case_row["answer_ref"] or "").startswith("cache://"))

        service.domjudge_update_judging(
            "judgehost-official",
            judgetask_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )

        meta_text = "cpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-official",
            judgetask_id,
            {
                "runresult": "correct",
                "runtime": "0.004",
                "output_run": "",
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )
        service.domjudge_add_debug_info(
            hostname="judgehost-official",
            judgetask_id=judgetask_id,
            payload={"level": "info", "message": "post-run debug payload"},
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = dict(run_row["summary"])
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("verdict") or ""), "OK")
        feedback_files = tests[0].get("feedback_files") if isinstance(tests[0], dict) else []
        self.assertIsInstance(feedback_files, list)
        self.assertTrue(feedback_files)
        first_feedback_token = str(feedback_files[0] or "")
        self.assertTrue(first_feedback_token.startswith("cache://") or first_feedback_token.startswith("feedback_dir/001/"))
        self.assertEqual(service.resolve_artifact_blob(first_feedback_token), b"ok\n")
        passes = tests[0].get("passes") if isinstance(tests[0], dict) else []
        self.assertIsInstance(passes, list)
        self.assertTrue(passes)
        first_pass = passes[0] if isinstance(passes[0], dict) else {}
        self.assertTrue(str(first_pass.get("feedback") or "").strip())
        host_rows = service.domjudge_list_hosts()
        self.assertTrue(host_rows)
        self.assertEqual(str(host_rows[0].get("hostname") or ""), "judgehost-official")
        status = service.status()
        hosts = status.get("hosts") if isinstance(status, dict) else []
        host = next((item for item in hosts if str(item.get("hostname") or "") == "judgehost-official"), {})
        self.assertEqual(str(host.get("last_action") or ""), "debug")

    def test_domjudge_executable_files_require_live_job_memory(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-script-provider-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-script-provider-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-script-provider"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        service.domjudge_register_host("judgehost-script-provider")
        leased = service.domjudge_fetch_work("judgehost-script-provider", max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, leased, task_id)), None)
        self.assertIsNotNone(task_row)
        assert task_row is not None
        compare_script_id = int(task_row.get("compare_script_id") or 0)
        self.assertGreater(compare_script_id, 0)
        fresh_service = self._fresh_judgehost_service()
        with self.assertRaises(RuntimeError):
            fresh_service.domjudge_get_executable_files("compare", compare_script_id)

    def test_domjudge_executable_files_ignore_deleted_job_work_roots(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-script-cache-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-script-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-script-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id_a = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-script-cache-a"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=True,
        )
        service.domjudge_register_host("judgehost-script-cache-a")
        leased_a = service.domjudge_fetch_work("judgehost-script-cache-a", max_batchsize=8)
        row_a = next(iter(self._work_rows_for_task(service, leased_a, task_id_a)), None)
        self.assertIsNotNone(row_a)
        assert row_a is not None
        case_id_a = int(row_a.get("judgetaskid") or 0)
        compare_script_id_a = int(row_a.get("compare_script_id") or 0)

        service.domjudge_update_judging(
            "judgehost-script-cache-a",
            case_id_a,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            "judgehost-script-cache-a",
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_run": "",
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": "",
                "compare_metadata": "",
            },
        )
        self.assertEqual(service.wait_for_task(task_id_a, timeout_sec=2.0), run_id_a)

        task_id_b = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-script-cache-b"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=True,
        )
        service.domjudge_register_host("judgehost-script-cache-b")
        leased_b: list[dict[str, object]] = []
        row_b = None
        for _ in range(8):
            leased_b = service.domjudge_fetch_work("judgehost-script-cache-b", max_batchsize=8)
            row_b = next(iter(self._work_rows_for_task(service, leased_b, task_id_b)), None)
            if row_b is not None:
                break
        self.assertIsNotNone(row_b)
        assert row_b is not None
        compare_script_id_b = int(row_b.get("compare_script_id") or 0)
        self.assertEqual(compare_script_id_b, compare_script_id_a)

        shutil.rmtree(service.toolkit.work_root(task_id_b), ignore_errors=True)

        compare_files = service.domjudge_get_executable_files(
            "compare",
            compare_script_id_b,
            hostname="judgehost-script-cache-b",
        )
        compare_names = {str(item.get("filename") or "") for item in compare_files}
        self.assertIn("run", compare_names)

    def test_domjudge_missing_executable_cache_fails_active_job(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-script-miss-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-script-miss-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-script-miss"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        host = "judgehost-script-miss"
        service.domjudge_register_host(host)
        leased = service.domjudge_fetch_work(host, max_batchsize=8)
        row = next(iter(self._work_rows_for_task(service, leased, task_id)), None)
        self.assertIsNotNone(row)
        assert row is not None
        case_id = int(row.get("judgetaskid") or 0)
        compare_script_id = int(row.get("compare_script_id") or 0)
        self.assertGreater(compare_script_id, 0)

        service.domjudge_update_judging(
            host,
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        batch_row = service.state.batch_scheduler.batch_for_task(task_id)
        self.assertIsNotNone(batch_row)
        assert batch_row is not None
        with patch.object(service.toolkit, "read_executable_cache", return_value=None):
            with self.assertRaises(RuntimeError):
                service.domjudge_get_executable_files(
                    "compare",
                    compare_script_id,
                    hostname=host,
                )

        failed_batch = judgehost_fetch_batch(service, int(batch_row["batch_id"] or 0))
        self.assertIsNotNone(failed_batch)
        assert failed_batch is not None
        self.assertEqual(str(failed_batch["status"] or ""), "open")
        service.schedule_verification_cleanup(str(failed_batch["verification_id"]))
        failed_batch = judgehost_fetch_batch(service, int(batch_row["batch_id"] or 0))
        assert failed_batch is not None
        self.assertEqual(str(failed_batch["status"] or ""), "failed")

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        assert run_row is not None
        self.assertEqual(str(run_row["status"] or ""), "failed")
        self.assertIn(
            "judgehost executable cache missing: compare/",
            str(dict(run_row["summary"]).get("error") or ""),
        )

    def test_generate_prepared_payload_recomputes_domjudge_precomputed_from_final_verification_payload(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-generate-recompute-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-generate-recompute-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        validator_source = (
            "#include \"testlib.h\"\n"
            "int main(){\n"
            "  registerValidation();\n"
            "  inf.readInt();\n"
            "  inf.readEof();\n"
            "  return 0;\n"
            "}\n"
        ).encode("utf-8")
        prepared = _prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id,
            test_name="001.in",
            input_bytes=b"\"$SUBMISSION_BIN\" 4\n",
            answer_bytes=b"",
            verification_payload_base={
                "run_config_json": json.dumps(
                    {
                        "checker_mode": "testlib",
                        "checker_args": [],
                        "pass_limit": 1,
                        "time_limit_ms": 30000,
                        "memory_limit_mb": 1024,
                    },
                    separators=(",", ":"),
                ),
                "problem_limits": {
                    "time_limit_ms": 30000,
                    "memory_limit_mb": 1024,
                    "pass_limit": 1,
                },
                "binaries_b64": {},
                "sources_b64": {
                    "validator.cpp": base64.b64encode(validator_source).decode("ascii"),
                    "testlib.h": base64.b64encode(b"").decode("ascii"),
                },
            },
            extra_sources_b64={"testlib.h": base64.b64encode(b"").decode("ascii")},
            manual_validate_only=False,
        )
        self.assertNotIn("domjudge_precomputed", prepared)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(int argc,char**argv){return 0;}\n",
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-generate-recompute"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="verification.generate-input",
            task_kind="generate",
            bypass_case_result_cache=False,
            compile_only=False,
            persist_verification_run=False,
            prepared_payload=prepared,
        )
        service.domjudge_register_host("judgehost-generate-recompute")
        leased = service.domjudge_fetch_work("judgehost-generate-recompute", max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, leased, task_id)), None)
        self.assertIsNotNone(task_row)
        assert task_row is not None
        compare_files = service.domjudge_get_executable_files("compare", str(task_row.get("compare_script_id") or ""))
        compare_names = {str(item.get("filename") or "") for item in compare_files}
        self.assertIn("run", compare_names)
        self.assertIn("validator.cpp", compare_names)

    def test_domjudge_selected_tests_not_truncated_by_max_tests_per_task(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        old_max_tests_per_task = service.state.max_tests_per_task
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        self.addCleanup(setattr, service.state, "max_tests_per_task", old_max_tests_per_task)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True
        service.state.max_tests_per_task = 1

        verification_id = f"b-jh-dom-notrunc-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-notrunc-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(
            verification_id,
            [("001.in", "ok\n", "ok\n"), ("002.in", "second\n", "second\n")],
        )

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in", "002.in"],
            verification_id=_canonical_verification_id("inv-domjudge-notrunc"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="build.solve",
        )

        service.domjudge_register_host("judgehost-notrunc")
        rows = service.domjudge_fetch_work("judgehost-notrunc", max_batchsize=8)
        self.assertEqual(len(rows), 1)
        service.domjudge_update_judging(
            "judgehost-notrunc",
            int(rows[0]["judgetaskid"]),
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        rows += service.domjudge_fetch_work("judgehost-notrunc", max_batchsize=8)
        self.assertEqual(len(rows), 2)
        inputs_seen: set[str] = set()
        for row in rows:
            testcase_id = int(row.get("testcase_id") or 0)
            self.assertGreater(testcase_id, 0)
            files = service.domjudge_get_testcase_files(testcase_id, hostname="judgehost-notrunc")
            self.assertEqual({str(item.get("filename") or "") for item in files}, {"input", "output"})
            input_blob = next((str(item.get("content") or "") for item in files if str(item.get("filename") or "") == "input"), "")
            input_text = base64.b64decode(input_blob).decode("utf-8", errors="replace")
            inputs_seen.add(input_text)
        self.assertEqual(inputs_seen, {"ok\n", "second\n"})

    def test_domjudge_reuses_script_ids_for_same_hash_payload(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-dom-cache-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-dom-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-dom-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-cache-b"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-cache"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=True,
        )

        service.domjudge_register_host("judgehost-official-cache")

        rows_a = service.domjudge_fetch_work("judgehost-official-cache", max_batchsize=8)
        self.assertEqual(len(rows_a), 1)
        row_a = rows_a[0]
        self.assertEqual(str(row_a.get("type") or ""), "judging_run")
        judgetask_id_a = int(row_a.get("judgetaskid") or 0)
        compile_id_a = int(row_a.get("compile_script_id") or 0)
        run_id_num_a = int(row_a.get("run_script_id") or 0)
        compare_id_a = int(row_a.get("compare_script_id") or 0)
        self.assertGreater(judgetask_id_a, 0)
        self.assertGreater(compile_id_a, 0)
        self.assertGreater(run_id_num_a, 0)
        self.assertGreater(compare_id_a, 0)

        service.domjudge_update_judging(
            "judgehost-official-cache",
            judgetask_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-official-cache",
            judgetask_id_a,
            {
                "runresult": "correct",
                "runtime": "0.004",
                "output_run": "",
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        rows_b = []
        for _ in range(8):
            rows_b = service.domjudge_fetch_work("judgehost-official-cache", max_batchsize=8)
            if rows_b:
                break
        self.assertEqual(len(rows_b), 1)
        row_b = rows_b[0]
        self.assertEqual(str(row_b.get("type") or ""), "judging_run")
        compile_id_b = int(row_b.get("compile_script_id") or 0)
        run_id_num_b = int(row_b.get("run_script_id") or 0)
        compare_id_b = int(row_b.get("compare_script_id") or 0)

        self.assertNotEqual(row_b["jobid"], row_a["jobid"])
        self.assertEqual(row_b["submitid"], row_a["submitid"])
        self.assertEqual(row_b["uuid"], row_a["uuid"])
        self.assertEqual(compile_id_b, compile_id_a)
        self.assertEqual(run_id_num_b, run_id_num_a)
        self.assertEqual(compare_id_b, compare_id_a)

    def test_domjudge_multi_pass_summary_keeps_single_final_pass_and_strips_protocol_output(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-dom-mp-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-mp-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        metadata = config.verification_service.verification_detail(verification_id)
        metadata["run_config_json"] = json.dumps({"checker_mode": "testlib", "checker_args": [], "pass_limit": 2})
        config.verification_service.persist_verification_detail(verification_id, metadata)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-mp"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        service.domjudge_register_host("judgehost-mp")
        tasks = service.domjudge_fetch_work("judgehost-mp", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        judgetask_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(judgetask_id, 0)

        service.domjudge_update_judging(
            "judgehost-mp",
            judgetask_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        noisy_output = (
            b"[  0.019s/6]>: 1 100\n"
            b"hello\n"
            b"[  0.054s/4]<: ? 0\n"
        )
        meta_text = "cpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-mp",
            judgetask_id,
            {
                "runresult": "correct",
                "runtime": "0.004",
                "output_run": base64.b64encode(noisy_output).decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = dict(run_row["summary"])
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        row = tests[0] if isinstance(tests[0], dict) else {}
        passes = row.get("passes") if isinstance(row, dict) else []
        self.assertIsInstance(passes, list)
        self.assertEqual(len(passes), 1)
        self.assertEqual(str((passes[0] or {}).get("verdict") or ""), "OK")
        output_ref = str((passes[0] or {}).get("output_ref") or "").strip()
        self.assertTrue(output_ref)
        self.assertEqual(service.resolve_artifact_blob(output_ref), noisy_output)
        feedback_files = row.get("feedback_files") if isinstance(row, dict) else []
        self.assertTrue(feedback_files)
        first_feedback_token = str(feedback_files[0] or "")
        self.assertTrue(first_feedback_token.endswith("judgemessage.txt"))
        self.assertEqual(service.resolve_artifact_blob(first_feedback_token), b"ok\n")

        run_root = self._verification_artifact_root(verification_id) / "runs" / run_id
        self.assertFalse((run_root / "001.out").exists())

    def test_domjudge_rewrites_untrusted_non_tl_result_when_cpu_exceeds_time_limit(self) -> None:
        self.assertEqual(
            domjudge_rewrite_untrusted_runresult(
                "wrong-answer",
                cpu_sec=6.5,
                run_cfg_obj={"time_limit": 6.0},
            ),
            "timelimit",
        )
        self.assertEqual(
            domjudge_rewrite_untrusted_runresult(
                "run-error",
                cpu_sec=6.1,
                run_cfg_obj={"time_limit_ms": 6000},
            ),
            "timelimit",
        )
        self.assertEqual(
            domjudge_rewrite_untrusted_runresult(
                "wrong-answer",
                cpu_sec=5.9,
                run_cfg_obj={"time_limit": 6.0},
            ),
            "wrong-answer",
        )
        self.assertEqual(
            domjudge_rewrite_untrusted_runresult(
                "correct",
                cpu_sec=15.0,
                run_cfg_obj={"time_limit": 6.0},
            ),
            "correct",
        )
        self.assertEqual(
            domjudge_rewrite_untrusted_runresult(
                "run-error",
                cpu_sec=0.6,
                run_cfg_obj={"time_limit": 0.5},
            ),
            "timelimit",
        )
        self.assertEqual(
            domjudge_rewrite_untrusted_runresult(
                "run-error",
                cpu_sec=0.4,
                run_cfg_obj={"time_limit": 0.5},
            ),
            "run-error",
        )

    def test_domjudge_add_judging_run_rewrites_wa_to_tl_on_double_cpu(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-dom-wa2tl-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-wa2tl-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        metadata = config.verification_service.verification_detail(verification_id)
        metadata["run_config_json"] = json.dumps({"checker_mode": "testlib", "checker_args": [], "time_limit_ms": 6000})
        config.verification_service.persist_verification_detail(verification_id, metadata)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-wa2tl"),
            verification_run_ids=[run_id],
            expected_behavior="unknown",
            verification_source="run.execute",
        )
        service.domjudge_register_host("judgehost-wa2tl")
        tasks = service.domjudge_fetch_work("judgehost-wa2tl", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        judgetask_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(judgetask_id, 0)

        service.domjudge_update_judging(
            "judgehost-wa2tl",
            judgetask_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 13.0\nwall-time: 13.5\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-wa2tl",
            judgetask_id,
            {
                "runresult": "wrong-answer",
                "runtime": "13.0",
                "output_run": base64.b64encode(b"bad\n").decode("ascii"),
                "output_diff": base64.b64encode(b"wa\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = dict(run_row["summary"])
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        test_row = tests[0] if isinstance(tests[0], dict) else {}
        self.assertEqual(str(test_row.get("verdict") or ""), "TL")
        passes = test_row.get("passes") if isinstance(test_row, dict) else []
        self.assertIsInstance(passes, list)
        self.assertEqual(str((passes[0] or {}).get("verdict") or ""), "TL")

    def test_domjudge_register_host_is_idempotent_for_leased_job(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-dom-reconnect-{uuid.uuid4().hex[:8]}"
        execution_verification_id = _canonical_verification_id("inv-domjudge-reconnect")
        run_id = f"r-jh-dom-reconnect-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=execution_verification_id,
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertTrue(task_id.startswith("jt-"))

        service.domjudge_register_host("judgehost-reconnect")
        first_rows = service.domjudge_fetch_work("judgehost-reconnect", max_batchsize=8)
        self.assertEqual(len(first_rows), 1)
        first = first_rows[0]
        self.assertEqual(
            set(first),
            {
                "type",
                "judgetaskid",
                "jobid",
                "uuid",
                "submitid",
                "contestid",
                "compile_script_id",
                "run_script_id",
                "compare_script_id",
                "testcase_id",
                "testcase_hash",
                "compile_config",
                "run_config",
                "compare_config",
            },
        )
        protocol_job_id = int(first.get("jobid") or 0)
        self.assertEqual(protocol_job_id, domjudge_job_id(execution_verification_id))
        self.assertRegex(str(first.get("submitid") or ""), r"^[0-9]+$")
        self.assertRegex(str(first.get("uuid") or ""), r"^[0-9a-f]{64}$")
        self.assertEqual(int(first["submitid"]), domjudge_submit_id(str(first["uuid"])))

        unfinished = service.domjudge_register_host("judgehost-reconnect")
        self.assertEqual(unfinished, [])

        second_rows = service.domjudge_fetch_work("judgehost-reconnect", max_batchsize=8)
        self.assertEqual(second_rows, [])

        case_row = judgehost_fetch_case(service, int(first["judgetaskid"]))
        self.assertIsNotNone(case_row)
        assert case_row is not None
        batch_row = judgehost_fetch_batch(service, int(case_row["batch_id"]))
        self.assertIsNotNone(batch_row)
        assert batch_row is not None
        self.assertEqual(str(batch_row["compile_owner"] or ""), "judgehost-reconnect")
        self.assertEqual(str(batch_row["status"] or ""), "open")
        self.assertEqual(str(case_row["lease_owner"] or ""), "judgehost-reconnect")
        self.assertEqual(str(case_row["status"] or ""), "leased")

    def test_domjudge_fetch_work_refreshes_heartbeat_for_active_idle_job(self) -> None:
        service = config.judgehost_task_service
        verification_id = f"ver-{uuid.uuid4().hex}"
        run_id = f"r-active-idle-heartbeat-{uuid.uuid4().hex[:8]}"
        host = "judgehost-active-idle-heartbeat"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(verification_id, [("001.in", "ok\n", "ok\n")])

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=f"ver-{uuid.uuid4().hex}",
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertTrue(task_id.startswith("jt-"))

        service.domjudge_register_host(host)
        first_rows = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(first_rows), 1)

        with service.state.state_lock:
            service.state.hosts_state[host]["last_seen_at"] = "2000-01-01T00:00:00+00:00"

        second_rows = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(second_rows, [])

        host_rows = {
            str(row.get("hostname") or ""): row
            for row in service.status().get("hosts", [])
        }
        self.assertIn(host, host_rows)
        self.assertTrue(host_rows[host].get("online"))
        self.assertEqual(host_rows[host].get("last_action"), "fetch")

    def test_enqueue_rejects_invalid_payload_before_scheduling(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"

        build_bad = f"b-jh-dom-bad-{uuid.uuid4().hex[:8]}"
        run_bad = f"r-jh-dom-bad-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_bad)
        service.state.include_build_payload = False
        with self.assertRaisesRegex(RuntimeError, "no tests in judgehost payload"):
            service.enqueue_task(
                problem=self.problem,
                username=self.user,
                artifact_verification_id=build_bad,
                mode="pass-fail",
                submission_path="solutions/ac.cpp",
                upload_content=None,
                upload_filename=None,
                run_id=run_bad,
                selected_tests=["001.in"],
                verification_id=_canonical_verification_id("inv-domjudge-bad"),
                verification_run_ids=[run_bad],
                expected_behavior="accepted",
                verification_source="run.execute",
            )

        build_good = f"b-jh-dom-good-{uuid.uuid4().hex[:8]}"
        run_good = f"r-jh-dom-good-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_good)
        service.state.include_build_payload = True
        good_task = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=build_good,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_good,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-good"),
            verification_run_ids=[run_good],
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        service.domjudge_register_host("judgehost-skip-invalid")
        tasks = service.domjudge_fetch_work("judgehost-skip-invalid", max_batchsize=8)
        self.assertTrue(tasks)
        self.assertEqual(len(self._work_rows_for_task(service, tasks, good_task)), 1)

        bad_task_row = service.state.task_registry.get_for_run(run_bad)
        self.assertIsNotNone(bad_task_row)
        self.assertEqual(str(bad_task_row.get("status") or ""), service.STATUS_FAILED)
        self.assertIn("no tests in judgehost payload", str(bad_task_row.get("error_text") or ""))

    def test_domjudge_reuses_cached_test_hash_but_exposes_real_test_number_as_testcase_id(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        build_a = f"b-jh-cache-a-{uuid.uuid4().hex[:8]}"
        run_a = f"r-jh-cache-a-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_a)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=build_a,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-cache-a"),
            verification_run_ids=[run_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        service.domjudge_register_host("judgehost-cache-a")
        rows_a = service.domjudge_fetch_work("judgehost-cache-a", max_batchsize=8)
        self.assertEqual(len(rows_a), 1)
        testcase_id_a = int(rows_a[0].get("testcase_id") or 0)
        case_id_a = int(rows_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        self.assertEqual(testcase_id_a, case_id_a)
        row_a = judgehost_fetch_case(service, case_id_a)
        self.assertIsNotNone(row_a)
        cached_testcase_id_a = int(row_a["testcase_id"] or 0)
        self.assertEqual(cached_testcase_id_a, 1)

        build_b = f"b-jh-cache-b-{uuid.uuid4().hex[:8]}"
        run_b = f"r-jh-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_b)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=build_b,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-cache-b"),
            verification_run_ids=[run_b],
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        service.domjudge_register_host("judgehost-cache-b")
        rows_b = service.domjudge_fetch_work("judgehost-cache-b", max_batchsize=8)
        self.assertEqual(len(rows_b), 1)
        testcase_id_b = int(rows_b[0].get("testcase_id") or 0)
        case_id_b = int(rows_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertEqual(testcase_id_b, case_id_b)
        self.assertNotEqual(case_id_a, case_id_b)
        row_b = judgehost_fetch_case(service, case_id_b)
        self.assertIsNotNone(row_b)
        cached_testcase_id_b = int(row_b["testcase_id"] or 0)
        self.assertEqual(cached_testcase_id_b, 1)
        self.assertEqual(cached_testcase_id_a, cached_testcase_id_b)

    def test_domjudge_testcase_files_resolve_by_host_lease_for_real_test_number(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        build_a = f"b-jh-host-a-{uuid.uuid4().hex[:8]}"
        run_a = f"r-jh-host-a-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_a)
        self._seed_verification_test_artifacts(build_a, [("001.in", "alpha\n", "alpha-out\n")])
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=build_a,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-host-a"),
            verification_run_ids=[run_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        build_b = f"b-jh-host-b-{uuid.uuid4().hex[:8]}"
        run_b = f"r-jh-host-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_b)
        self._seed_verification_test_artifacts(build_b, [("001.in", "beta\n", "beta-out\n")])
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=build_b,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-host-b"),
            verification_run_ids=[run_b],
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        service.domjudge_register_host("judgehost-host-a")
        service.domjudge_register_host("judgehost-host-b")
        rows_a = service.domjudge_fetch_work("judgehost-host-a", max_batchsize=1)
        rows_b = service.domjudge_fetch_work("judgehost-host-b", max_batchsize=1)
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(len(rows_b), 1)
        testcase_id_a = int(rows_a[0].get("testcase_id") or 0)
        testcase_id_b = int(rows_b[0].get("testcase_id") or 0)
        self.assertEqual(testcase_id_a, int(rows_a[0].get("judgetaskid") or 0))
        self.assertEqual(testcase_id_b, int(rows_b[0].get("judgetaskid") or 0))

        files_a = service.domjudge_get_testcase_files(testcase_id_a, hostname="judgehost-host-a")
        files_b = service.domjudge_get_testcase_files(testcase_id_b, hostname="judgehost-host-b")
        input_a = base64.b64decode(next(item["content"] for item in files_a if str(item.get("filename") or "") == "input")).decode("utf-8", errors="replace")
        input_b = base64.b64decode(next(item["content"] for item in files_b if str(item.get("filename") or "") == "input")).decode("utf-8", errors="replace")
        self.assertEqual(input_a, "alpha\n")
        self.assertEqual(input_b, "beta\n")







    def test_domjudge_interactive_uses_configured_pass_limit(self) -> None:
        service = config.judgehost_task_service
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.include_build_payload = True

        verification_id = f"b-jh-passlimit-interactive-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-passlimit-interactive-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        ws = Path(self._workspace_path())
        (ws / "interactors").mkdir(parents=True, exist_ok=True)
        (ws / "interactors" / "interactor.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )
        metadata = config.verification_service.verification_detail(verification_id)
        metadata["run_config_json"] = json.dumps({"checker_mode": "testlib", "checker_args": [], "pass_limit": 7})
        config.verification_service.persist_verification_detail(verification_id, metadata)

        payload = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-passlimit-interactive"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        precomputed = payload.get("domjudge_precomputed") if isinstance(payload, dict) else {}
        run_cfg = precomputed.get("run_config") if isinstance(precomputed, dict) else {}
        self.assertIsInstance(run_cfg, dict)
        self.assertEqual(int(run_cfg.get("pass_limit") or 0), 7)

    def test_domjudge_pass_fail_multi_pass_uses_configured_pass_limit(self) -> None:
        service = config.judgehost_task_service
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.include_build_payload = True

        verification_id = f"b-jh-passlimit-multipass-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-passlimit-multipass-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        metadata = config.verification_service.verification_detail(verification_id)
        metadata["run_config_json"] = json.dumps({"checker_mode": "testlib", "checker_args": [], "pass_limit": 7})
        config.verification_service.persist_verification_detail(verification_id, metadata)

        payload = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-passlimit-pass-fail"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        precomputed = payload.get("domjudge_precomputed") if isinstance(payload, dict) else {}
        run_cfg = precomputed.get("run_config") if isinstance(precomputed, dict) else {}
        self.assertIsInstance(run_cfg, dict)
        self.assertEqual(int(run_cfg.get("pass_limit") or 0), 7)

    def test_domjudge_interactor_source_overrides_host_binary_payload(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        ws = Path(self._workspace_path())
        (ws / "interactors").mkdir(parents=True, exist_ok=True)
        (ws / "interactors" / "interactor.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )

        verification_id = f"b-jh-interactor-source-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-interactor-source-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        artifact_root = self._verification_artifact_root(verification_id)
        interactor_bin = artifact_root / "bin" / "interactor"
        interactor_bin.parent.mkdir(parents=True, exist_ok=True)
        interactor_bin.write_bytes(b"\x7fELFfake-host-interactor")
        os.chmod(interactor_bin, 0o755)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-jh-interactor-source"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
            compile_only=False,
        )

        host = "judgehost-interactor-source"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, tasks, task_id)), None)
        self.assertIsNotNone(task_row)

        run_files = service.domjudge_get_executable_files("run", str(task_row.get("run_script_id") or ""))
        run_names = {str(item.get("filename") or "") for item in run_files}
        self.assertIn("build", run_names)
        self.assertIn("interactor.cpp", run_names)
        self.assertIn("testlib.h", run_names)
        self.assertNotIn("run", run_names)
        build_item = next((item for item in run_files if str(item.get("filename") or "") == "build"), {})
        build_text = base64.b64decode(str(build_item.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("-DDOMJUDGE", build_text)
        self.assertIn("interactor.cpp", build_text)



    def test_domjudge_java_compile_payload_includes_detect_main_source(self) -> None:
        service = config.judgehost_task_service
        precomputed = service.enqueue._domjudge_precomputed_fields_from_payload(
            {
                "source_name": "TranslateMain.java",
                "source_b64": base64.b64encode(
                    b"public class TranslateMain { public static void main(String[] args) {} }\n"
                ).decode("ascii"),
                "entry_point": "TranslateMain",
                "verification_payload": {
                    "tests": [
                        {
                            "name": "001.in",
                            "input_b64": "",
                            "answer_b64": "",
                        }
                    ],
                    "run_config_json": "{}",
                    "problem_limits": {},
                    "binaries_b64": {},
                    "sources_b64": {},
                },
                "mode": "pass-fail",
                "verification_source": "run.execute",
            }
        )
        compile_files = list(precomputed.get("compile_files") or [])
        file_names = [str(name) for name, _content, _is_exec in compile_files]
        self.assertIn("run", file_names)
        self.assertIn("DetectMain.java", file_names)

    def test_prepare_enqueue_payload_renames_java_source_to_detected_entry_point(self) -> None:
        service = config.judgehost_task_service
        payload = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id="",
            mode="pass-fail",
            submission_path=None,
            upload_content=(
                b"public class TranslateMain {\n"
                b"  public static void main(String[] args) {}\n"
                b"}\n"
            ),
            upload_filename="java_translate.java",
            run_id=f"r-java-entry-{uuid.uuid4().hex[:8]}",
            selected_tests=[],
            verification_id=f"ver-{uuid.uuid4().hex}",
            verification_run_ids=[],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertEqual(str(payload.get("source_name") or ""), "TranslateMain.java")
        self.assertEqual(str(payload.get("entry_point") or ""), "TranslateMain")
        precomputed = dict(payload.get("domjudge_precomputed") or {})
        run_config = dict(precomputed.get("run_config") or {})
        self.assertEqual(str(run_config.get("entry_point") or ""), "TranslateMain")

    def test_domjudge_java_upload_is_sent_with_detected_entry_point_filename(self) -> None:
        service = self._fresh_judgehost_service()
        verification_id = f"ver-{uuid.uuid4().hex}"
        run_id = f"r-java-upload-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=(
                b"public class TranslateMain {\n"
                b"  public static void main(String[] args) {}\n"
                b"}\n"
            ),
            upload_filename="java_translate.java",
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=verification_id,
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
            persist_verification_run=False,
        )
        service.domjudge_register_host("judgehost-java-upload")
        leased = service.domjudge_fetch_work("judgehost-java-upload", max_batchsize=1)
        self.assertEqual(len(leased), 1)
        row = leased[0]
        self.assertEqual(len(self._work_rows_for_task(service, [row], task_id)), 1)
        submit_id = str(row.get("submitid") or "")
        contest_id = str(row.get("contestid") or "")
        source_files = service.domjudge_get_source_files(submit_id, contest_id=contest_id)
        self.assertTrue(source_files)
        self.assertEqual(str(source_files[0].get("filename") or ""), "TranslateMain.java")

    def test_prepare_enqueue_payload_rejects_java_without_runnable_main_class(self) -> None:
        service = config.judgehost_task_service
        with self.assertRaisesRegex(RuntimeError, "no runnable main class found"):
            service.prepare_enqueue_payload(
                problem=self.problem,
                username=self.user,
                artifact_verification_id="",
                mode="pass-fail",
                submission_path=None,
                upload_content=b"class Helper {}\n",
                upload_filename="helper.java",
                run_id=f"r-java-missing-main-{uuid.uuid4().hex[:8]}",
                selected_tests=[],
                verification_id=f"ver-{uuid.uuid4().hex}",
                verification_run_ids=[],
                expected_behavior="accepted",
                verification_source="run.execute",
            )

    def test_domjudge_config_uses_kib_for_script_filesize_and_bytes_for_output_storage(self) -> None:
        service = config.judgehost_task_service
        cfg = service.domjudge_config()
        run_output_bytes = int(getattr(service.state.constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536) * 1024
        compile_output_kb = int(getattr(service.state.constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 262144) or 262144)
        stored_log_limit_bytes = int(getattr(service.state.constants, "JUDGEHOST_STORED_LOG_LIMIT_BYTES", 65536) or 65536)
        aux_limit_bytes = int(getattr(service.state.constants, "AUX_DISPLAY_TEXT_LIMIT_BYTES", 2048) or 2048)
        self.assertEqual(str(cfg.get("timelimit_overshoot") or ""), "1s|100%")
        self.assertEqual(
            int(cfg.get("output_storage_limit") or 0),
            run_output_bytes,
        )
        self.assertEqual(
            int(cfg.get("script_filesize_limit") or 0),
            compile_output_kb,
        )
        self.assertGreaterEqual(int(cfg.get("script_filesize_limit") or 0), 1024)
        self.assertLess(stored_log_limit_bytes, int(cfg.get("output_storage_limit") or 0))
        self.assertNotEqual(
            int(cfg.get("output_storage_limit") or 0),
            int(cfg.get("script_filesize_limit") or 0),
        )
        self.assertNotEqual(
            int(cfg.get("script_filesize_limit") or 0),
            (aux_limit_bytes + 1023) // 1024,
        )















    def test_domjudge_generate_verification_uses_generate_scripts_and_validator_payload(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-generate-scripts-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-generate-scripts-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        ws = Path(self._workspace_path())
        (ws / "validators").mkdir(parents=True, exist_ok=True)
        (ws / "validators" / "validator.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            encoding="utf-8",
        )

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-jh-generate-scripts"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            compile_only=False,
        )
        host = "judgehost-generate-scripts"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, tasks, task_id)), None)
        self.assertIsNotNone(task_row)

        run_files = service.domjudge_get_executable_files("run", str(task_row.get("run_script_id") or ""))
        run_item = next((item for item in run_files if str(item.get("filename") or "") == "run"), {})
        run_text = base64.b64decode(str(run_item.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("missing generate command payload", run_text)

        compare_files = service.domjudge_get_executable_files("compare", str(task_row.get("compare_script_id") or ""))
        compare_run = next((item for item in compare_files if str(item.get("filename") or "") == "run"), {})
        compare_text = base64.b64decode(str(compare_run.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("VALIDATOR_BIN", compare_text)
        compare_names = {str(item.get("filename") or "") for item in compare_files}
        self.assertTrue("validator" in compare_names or "validator.cpp" in compare_names)

    def test_domjudge_generate_verification_interactive_mode_does_not_require_interactor_payload(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-generate-interactive-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-generate-interactive-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        ws = Path(self._workspace_path())
        (ws / "validators").mkdir(parents=True, exist_ok=True)
        (ws / "validators" / "validator.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            encoding="utf-8",
        )

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-jh-generate-interactive"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="build.generate-input",
            compile_only=False,
        )
        host = "judgehost-generate-interactive"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, tasks, task_id)), None)
        self.assertIsNotNone(task_row)

        run_files = service.domjudge_get_executable_files("run", str(task_row.get("run_script_id") or ""))
        run_names = {str(item.get("filename") or "") for item in run_files}
        self.assertIn("run", run_names)
        self.assertNotIn("interactor.cpp", run_names)
        run_item = next((item for item in run_files if str(item.get("filename") or "") == "run"), {})
        run_text = base64.b64decode(str(run_item.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("missing generate command payload", run_text)

        compare_files = service.domjudge_get_executable_files("compare", str(task_row.get("compare_script_id") or ""))
        compare_names = {str(item.get("filename") or "") for item in compare_files}
        self.assertTrue("validator" in compare_names or "validator.cpp" in compare_names)






    def test_domjudge_compile_only_uses_single_virtual_case_even_with_build_tests(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compile-only-virtual-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-virtual-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-virtual"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-virtual")
        tasks = service.domjudge_fetch_work("judgehost-compile-only-virtual", max_batchsize=16)
        task_rows = self._work_rows_for_task(service, tasks, task_id)
        self.assertEqual(len(task_rows), 1)
        case_id = int(task_rows[0].get("judgetaskid") or 0)
        testcase_id = int(task_rows[0].get("testcase_id") or 0)
        compare_script_id = str(task_rows[0].get("compare_script_id") or "")
        self.assertGreater(case_id, 0)
        self.assertEqual(testcase_id, case_id)
        compare_files = service.domjudge_get_executable_files("compare", compare_script_id)
        compare_run = next((item for item in compare_files if str(item.get("filename") or "") == "run"), {})
        compare_text = base64.b64decode(str(compare_run.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("exit 42", compare_text)
        db_rows = judgehost_cases_for_run(service, run_id)
        self.assertEqual(len(db_rows), 1)
        self.assertEqual(int(db_rows[0]["id"] or 0), case_id)
        self.assertEqual(str(db_rows[0]["test_name"] or ""), "compile-only.in")
        self.assertEqual(str(db_rows[0]["status"] or ""), "leased")

        service.domjudge_update_judging(
            "judgehost-compile-only-virtual",
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            "judgehost-compile-only-virtual",
            case_id,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_run": base64.b64encode(b"binary-artifact").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "output_diff": "",
                "metadata": "",
                "compare_metadata": "",
                "team_message": "",
            },
        )
        finished_run_id = service.wait_for_task(task_id, timeout_sec=2.0)
        self.assertEqual(finished_run_id, run_id)

    def test_domjudge_compile_only_multi_pass_with_interactor_stays_non_combined(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compile-only-multipass-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-multipass-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        artifact_root = self._verification_artifact_root(verification_id)
        interactor_bin = artifact_root / "bin" / "interactor"
        interactor_bin.parent.mkdir(parents=True, exist_ok=True)
        interactor_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(interactor_bin, 0o755)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-multipass"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        host = "judgehost-compile-only-multipass"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, tasks, task_id)), None)
        self.assertIsNotNone(task_row)

        compare_cfg = json.loads(str(task_row.get("compare_config") or "{}"))
        self.assertFalse(bool(compare_cfg.get("combined_run_compare")))

        run_files = service.domjudge_get_executable_files("run", str(task_row.get("run_script_id") or ""))
        run_item = next((item for item in run_files if str(item.get("filename") or "") == "run"), {})
        run_text = base64.b64decode(str(run_item.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn('cat "$TESTIN" >"$PROGOUT"', run_text)
        self.assertIn('"$@" </dev/null >/dev/null', run_text)
        self.assertNotIn("runpipe", run_text)

        case_id = int(task_row.get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)
        service.domjudge_update_judging(
            host,
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            host,
            case_id,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_run": base64.b64encode(b"binary-artifact").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "output_diff": "",
                "metadata": "",
                "compare_metadata": "",
                "team_message": "",
            },
        )
        finished_run_id = service.wait_for_task(task_id, timeout_sec=2.0)
        self.assertEqual(finished_run_id, run_id)

    def test_domjudge_compile_only_cache_hit_with_extra_sources(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        host = "judgehost-compile-only-extra-cache"
        verification_id = f"b-jh-compile-only-extra-cache-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        run_a = f"r-jh-compile-only-extra-a-{uuid.uuid4().hex[:8]}"
        extra_testlib = base64.b64encode(b"// testlib\n").decode("ascii")
        prepared = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_a,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-extra-a"),
            verification_run_ids=[run_a],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        prepared["extra_sources_b64"] = {"testlib.h": extra_testlib}

        task_a = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_a,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-extra-a"),
            verification_run_ids=[run_a],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
            prepared_payload=prepared,
        )
        service.domjudge_register_host(host)
        rows_a = service.domjudge_fetch_work(host, max_batchsize=16)
        task_rows_a = self._work_rows_for_task(service, rows_a, task_a)
        self.assertEqual(len(task_rows_a), 1)
        case_id_a = int(task_rows_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        service.domjudge_update_judging(
            host,
            case_id_a,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            host,
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_error": "",
                "output_system": "",
                "output_diff": "",
                "metadata": "",
                "compare_metadata": "",
                "team_message": "",
            },
        )
        self.assertEqual(service.wait_for_task(task_a, timeout_sec=2.0), run_a)
        run_row_a = self._verification_run_row(run_a)
        self.assertIsNotNone(run_row_a)

        run_b = f"r-jh-compile-only-extra-b-{uuid.uuid4().hex[:8]}"
        with (
            patch.object(
                service.toolkit,
                "cache_get_with_blobs",
                wraps=service.toolkit.cache_get_with_blobs,
            ) as cache_bundle_read,
        ):
            task_b = service.enqueue_task(
                problem=self.problem,
                username=self.user,
                artifact_verification_id=verification_id,
                mode="pass-fail",
                submission_path=None,
                upload_content=b"int main(){return 0;}\n",
                upload_filename="checker.cpp",
                run_id=run_b,
                selected_tests=[],
                verification_id=_canonical_verification_id("inv-jh-compile-only-extra-b"),
                verification_run_ids=[run_b],
                expected_behavior="compile",
                verification_source="build.compile",
                compile_only=True,
                prepared_payload=prepared,
            )
            rows_b = service.domjudge_fetch_work(host, max_batchsize=16)
            self.assertFalse(bool(self._work_rows_for_task(service, rows_b, task_b)))
            self.assertEqual(cache_bundle_read.call_count, 1)
        self.assertEqual(service.wait_for_task(task_b, timeout_sec=2.0), run_b)
        run_row_b = self._verification_run_row(run_b)
        self.assertIsNotNone(run_row_b)
        self.assertEqual(str(run_row_b["status"] or "").strip().lower(), "ok")
        self.assertEqual(run_row_b["summary"]["tests"], run_row_a["summary"]["tests"])

    def test_domjudge_compile_only_cache_hit_without_build_payload_tests(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        host = "judgehost-compile-only-empty-build-payload-cache"
        # save-source compile check path uses a placeholder build id and no build payload tests
        verification_id = "pending"

        run_a = f"r-jh-compile-only-empty-build-a-{uuid.uuid4().hex[:8]}"
        task_a = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="tmp.cpp",
            run_id=run_a,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-empty-build-a"),
            verification_run_ids=[run_a],
            expected_behavior="compile",
            verification_source="problem.solution.save_source",
            compile_only=True,
        )
        service.domjudge_register_host(host)
        rows_a = service.domjudge_fetch_work(host, max_batchsize=16)
        task_rows_a = self._work_rows_for_task(service, rows_a, task_a)
        self.assertEqual(len(task_rows_a), 1)
        case_id_a = int(task_rows_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        service.domjudge_update_judging(
            host,
            case_id_a,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            host,
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_error": "",
                "output_system": "",
                "output_diff": "",
                "metadata": "",
                "compare_metadata": "",
                "team_message": "",
            },
        )
        self.assertEqual(service.wait_for_task(task_a, timeout_sec=2.0), run_a)

        run_b = f"r-jh-compile-only-empty-build-b-{uuid.uuid4().hex[:8]}"
        task_b = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="tmp.cpp",
            run_id=run_b,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-empty-build-b"),
            verification_run_ids=[run_b],
            expected_behavior="compile",
            verification_source="problem.solution.save_source",
            compile_only=True,
        )
        rows_b = service.domjudge_fetch_work(host, max_batchsize=16)
        self.assertFalse(bool(self._work_rows_for_task(service, rows_b, task_b)))
        self.assertEqual(service.wait_for_task(task_b, timeout_sec=2.0), run_b)
        run_row_b = self._verification_run_row(run_b)
        self.assertIsNotNone(run_row_b)
        self.assertEqual(str(run_row_b["status"] or "").strip().lower(), "ok")

    def test_enqueue_task_uses_prepared_payload_without_collecting_fallback(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-prepared-merge-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-prepared-merge-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        extra_testlib = base64.b64encode(b"// testlib\n").decode("ascii")
        prepared = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-prepared-merge"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        prepared["extra_sources_b64"] = {"testlib.h": extra_testlib}

        with patch.object(
            service._enqueue,
            "_collect_verification_payload",
            side_effect=AssertionError("prepared enqueue collected fallback payload"),
        ), patch.object(
            service._enqueue._s.workspace_service,
            "workspace_context",
            side_effect=AssertionError("prepared enqueue loaded workspace context"),
        ):
            task_id = service.enqueue_task(
                problem=self.problem,
                username=self.user,
                artifact_verification_id=verification_id,
                mode="pass-fail",
                submission_path=None,
                upload_content=b"int main(){return 0;}\n",
                upload_filename="gen.cpp",
                run_id=run_id,
                selected_tests=[],
                verification_id=_canonical_verification_id("inv-jh-prepared-merge"),
                verification_run_ids=[run_id],
                expected_behavior="compile",
                verification_source="build.compile",
                compile_only=True,
                prepared_payload=prepared,
            )
        task = service.core.task_by_id(task_id)
        self.assertIsNotNone(task)
        payload = task.get("payload") if isinstance(task, dict) else {}
        self.assertIsInstance(payload, dict)
        self.assertEqual(str(payload.get("source_name") or ""), "gen.cpp")
        self.assertNotIn("source_b64", payload)
        self.assertIsInstance(payload.get("verification_payload"), dict)
        self.assertNotIn("extra_sources_b64", payload)
        self.assertTrue(bool(payload.get("compile_only")))

    def test_domjudge_source_files_include_prepared_extra_sources(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True
        self._reset_task_queue_state(service)

        verification_id = f"b-jh-extra-src-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-extra-src-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        extra_testlib = base64.b64encode(b"// testlib helper\n").decode("ascii")
        prepared = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b'#include "testlib.h"\nint main(){return 0;}\n',
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-extra-src"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        prepared["extra_sources_b64"] = {"testlib.h": extra_testlib}

        _task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b'#include "testlib.h"\nint main(){return 0;}\n',
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-extra-src"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
            prepared_payload=prepared,
        )
        service.domjudge_register_host("judgehost-extra-src")
        work_rows = service.domjudge_fetch_work("judgehost-extra-src", max_batchsize=16)
        self.assertTrue(work_rows)
        work_row = next(iter(self._work_rows_for_task(service, work_rows, _task_id)), None)
        self.assertIsNotNone(work_row)
        submit_id = str(work_row.get("submitid") or "")
        contest_id = str(work_row.get("contestid") or "")
        source_files = service.domjudge_get_source_files(submit_id, contest_id=contest_id)
        names = {str(item.get("filename") or "") for item in source_files}
        self.assertIn("gen.cpp", names)
        self.assertIn("testlib.h", names)

        case_id = int(work_row.get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)
        service.domjudge_update_judging(
            "judgehost-extra-src",
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            "judgehost-extra-src",
            case_id,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_error": "",
                "output_system": "",
                "output_diff": "",
                "metadata": "",
                "compare_metadata": "",
                "team_message": "",
            },
        )
        finished_run_id = service.wait_for_task(_task_id, timeout_sec=2.0)
        self.assertEqual(finished_run_id, run_id)

    def test_domjudge_compile_only_result_normalization_maps_success_to_ok(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compile-only-ok-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-ok-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        artifact_root = self._verification_artifact_root(verification_id)
        shutil.rmtree(artifact_root / "tests", ignore_errors=True)
        shutil.rmtree(artifact_root / "ans", ignore_errors=True)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-ok"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-ok")
        tasks = service.domjudge_fetch_work("judgehost-compile-only-ok", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compile-only-ok",
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            "judgehost-compile-only-ok",
            case_id,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_run": base64.b64encode(b"binary-artifact").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "output_diff": "",
                "metadata": "",
                "compare_metadata": "",
                "team_message": "",
            },
        )
        finished_run_id = service.wait_for_task(task_id, timeout_sec=2.0)
        self.assertEqual(finished_run_id, run_id)

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "ok")
        summary = dict(run_row["summary"])
        self.assertTrue(bool(summary.get("compile_only")))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(str((tests[0] or {}).get("verdict") or ""), "OK")

    def test_domjudge_compile_only_missing_output_is_normalized_to_ok(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compile-only-no-output-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-no-output-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        artifact_root = self._verification_artifact_root(verification_id)
        shutil.rmtree(artifact_root / "tests", ignore_errors=True)
        shutil.rmtree(artifact_root / "ans", ignore_errors=True)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-no-output"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-no-output")
        tasks = service.domjudge_fetch_work("judgehost-compile-only-no-output", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compile-only-no-output",
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        service.domjudge_add_judging_run(
            "judgehost-compile-only-no-output",
            case_id,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_error": "",
                "output_system": "",
                "output_diff": "",
                "metadata": "",
                "compare_metadata": "",
                "team_message": "",
            },
        )
        finished_run_id = service.wait_for_task(task_id, timeout_sec=2.0)
        self.assertEqual(finished_run_id, run_id)

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "ok")
        summary = dict(run_row["summary"])
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(str((tests[0] or {}).get("verdict") or ""), "OK")
        passes = (tests[0] or {}).get("passes") if tests else []
        first_pass = (passes[0] if isinstance(passes, list) and passes else {})
        self.assertFalse(str((first_pass or {}).get("output_ref") or "").strip())

    def test_domjudge_compile_only_result_normalization_maps_compile_failure_to_ce(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compile-only-ce-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-ce-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        artifact_root = self._verification_artifact_root(verification_id)
        shutil.rmtree(artifact_root / "tests", ignore_errors=True)
        shutil.rmtree(artifact_root / "ans", ignore_errors=True)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){ return syntax_error }\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-compile-only-ce"),
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-ce")
        tasks = service.domjudge_fetch_work("judgehost-compile-only-ce", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compile-only-ce",
            case_id,
            {"compile_success": "0", "output_compile": base64.b64encode(b"compile failed detail").decode("ascii"), "compile_metadata": ""},
        )
        with self.assertRaises(RuntimeError) as ctx:
            service.wait_for_task(task_id, timeout_sec=2.0)
        self.assertIn("compile failed detail", str(ctx.exception))

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        self.assertIn("compile failed detail", str(summary.get("error") or ""))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(str((tests[0] or {}).get("verdict") or ""), "CE")
        diagnostics = summary.get("compile_diagnostics") if isinstance(summary, dict) else []
        self.assertIsInstance(diagnostics, list)
        first_diag = diagnostics[0] if diagnostics else {}
        self.assertIn("compile failed detail", str((first_diag or {}).get("message") or ""))

    def test_domjudge_source_files_include_submission_extra_sources(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-extra-source-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-extra-source-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b'#include "testlib.h"\nint main(){return 0;}\n',
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=_canonical_verification_id("inv-jh-extra-source"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="build.generate-input",
            task_kind="generate",
            prepared_payload={
                "verification_payload": {
                    "tests": [
                        {
                            "name": "001.in",
                            "input_b64": base64.b64encode(b"1\n").decode("ascii"),
                            "answer_name": "001.ans",
                            "answer_b64": "",
                        }
                    ],
                    "run_config_json": json.dumps(
                        {
                            "checker_mode": "testlib",
                            "checker_args": [],
                            "pass_limit": 1,
                            "time_limit_ms": 30000,
                            "memory_limit_mb": 1024,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "problem_limits": {"time_limit_ms": 30000, "memory_limit_mb": 1024, "pass_limit": 1},
                    "binaries_b64": {},
                    "sources_b64": {},
                },
                "extra_sources_b64": {
                    "testlib.h": base64.b64encode(b"// fake testlib\n").decode("ascii"),
                },
            },
        )
        self.assertTrue(task_id)
        service.domjudge_register_host("judgehost-extra-source")
        tasks = service.domjudge_fetch_work("judgehost-extra-source", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        source_files = service.domjudge_get_source_files(str(tasks[0].get("submitid") or ""))
        source_names = {str(item.get("filename") or "") for item in source_files}
        self.assertIn("gen.cpp", source_names)
        self.assertIn("testlib.h", source_names)

    def test_domjudge_b64_decode_requires_base64_text(self) -> None:
        service = config.judgehost_task_service
        blob = b"ok\n"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(service.toolkit.b64_decode(encoded), blob)
        self.assertEqual(service.toolkit.b64_decode(encoded.encode("ascii")), blob)
        with self.assertRaises(RuntimeError):
            service.toolkit.b64_decode(b"binary-artifact")
        with self.assertRaises(RuntimeError):
            service.toolkit.b64_decode("%not-base64%")

    def test_domjudge_payload_blob_bytes_keeps_raw_upload_contract(self) -> None:
        service = config.judgehost_task_service
        blob = b"binary-artifact"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(service.toolkit.payload_blob_bytes(blob), blob)
        self.assertEqual(service.toolkit.payload_blob_bytes(encoded), blob)

    def test_domjudge_strip_protocol_trace_removes_runpipe_transcript_lines(self) -> None:
        cleaned = config.judgehost_task_service.toolkit.strip_protocol_trace(
            b"[  0.019s/6]>: 1 100\n"
            b"hello\n"
            b"[  0.054s/4]<: ? 0\n"
            b"[  0.071s/0]]\n"
            b"\n"
        )
        self.assertEqual(cleaned.decode("utf-8"), "hello\n")

    def test_domjudge_feedback_text_preserves_multiline_and_redacts_internal_path(self) -> None:
        from app.service.judgehost.runtime import (
            domjudge_feedback_text_from_bytes,
            domjudge_feedback_text_from_text,
        )

        self.assertEqual(
            domjudge_feedback_text_from_text("\n\nfailed on pass 2\nignored"),
            "failed on pass 2\nignored",
        )
        compile_output = (
            "\n"
            "/opt/domjudge/judgehost/judgings/judgedaemon-2-2/endpoint-default/executable/compare/123/"
            "b0e49bdbe272b5206d97ca5e888a7b00/build/validator.cpp: In function 'void EachTestCase()':\n"
            "/opt/domjudge/judgehost/judgings/judgedaemon-2-2/endpoint-default/executable/compare/123/"
            "b0e49bdbe272b5206d97ca5e888a7b00/build/validator.cpp:4:35: error: expected ';' before 'inf'\n"
        )
        self.assertEqual(
            domjudge_feedback_text_from_text(compile_output),
            "validator.cpp: In function 'void EachTestCase()':\nvalidator.cpp:4:35: error: expected ';' before 'inf'",
        )
        self.assertEqual(
            domjudge_feedback_text_from_bytes(compile_output.encode("utf-8")),
            "validator.cpp: In function 'void EachTestCase()':\nvalidator.cpp:4:35: error: expected ';' before 'inf'",
        )

    def test_domjudge_add_judging_run_endpoint_accepts_large_multipart_payload(self) -> None:
        from app.main import app

        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-large-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-large-{uuid.uuid4().hex[:8]}"
        large_output = b"A" * (20 * 1024 * 1024)
        metadata = b"cpu-time: 0.001\nwall-time: 0.001\nmemory-bytes: 4096\n"

        headers = {"Authorization": "Bearer test-token"}
        with TestClient(app) as client:
            self._seed_build_verification(verification_id)
            service.enqueue_task(
                problem=self.problem,
                username=self.user,
                artifact_verification_id=verification_id,
                mode="pass-fail",
                submission_path="solutions/ac.cpp",
                upload_content=None,
                upload_filename=None,
                run_id=run_id,
                selected_tests=["001.in"],
                verification_id=_canonical_verification_id("inv-domjudge-large"),
                verification_run_ids=[run_id],
                expected_behavior="accepted",
                verification_source="run.execute",
            )
            service.domjudge_register_host("judgehost-large")
            tasks = service.domjudge_fetch_work("judgehost-large", max_batchsize=1)
            self.assertEqual(len(tasks), 1)
            case_id = int(tasks[0].get("judgetaskid") or 0)
            self.assertGreater(case_id, 0)

            update_resp = client.put(
                f"/api/v4/judgehosts/update-judging/judgehost-large/{case_id}",
                data={
                    "compile_success": "1",
                    "output_compile": "",
                    "compile_metadata": "",
                },
                headers=headers,
            )
            self.assertEqual(update_resp.status_code, 200)

            add_resp = client.post(
                f"/api/v4/judgehosts/add-judging-run/judgehost-large/{case_id}",
                data={
                    "runresult": "correct",
                    "runtime": "0.001",
                },
                files={
                    "output_run": ("program.out", large_output, "application/octet-stream"),
                    "output_diff": ("judgemessage.txt", b"ok\n", "text/plain"),
                    "metadata": ("program.meta", metadata, "text/plain"),
                },
                headers=headers,
            )
            self.assertEqual(add_resp.status_code, 200)

            row = judgehost_fetch_case(service, case_id)
            self.assertIsNotNone(row)
            self.assertEqual(str(row["status"] or ""), "reported")
            self.assertEqual(str(row["runresult"] or ""), "correct")
            self.assertTrue(str(row["output_run_rel"] or "").startswith("cache://"))
            self.assertTrue(str(row["output_diff_rel"] or "").startswith("cache://"))
            self.assertTrue(str(row["metadata_rel"] or "").startswith("cache://"))

    def test_domjudge_compile_logs_are_truncated_before_state_storage(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compile-log-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-log-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-compile-log"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        service.domjudge_register_host("judgehost-compile-log")
        tasks = service.domjudge_fetch_work("judgehost-compile-log", max_batchsize=1)
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        assert case_row is not None
        batch_id = int(case_row["batch_id"])

        limit = int(getattr(service.state.constants, "JUDGEHOST_STORED_LOG_LIMIT_BYTES", 65536) or 65536)
        service.domjudge_update_judging(
            "judgehost-compile-log",
            case_id,
            {
                "compile_success": "1",
                "output_compile": b"A" * (limit + 8192),
                "compile_metadata": b"B" * (limit + 4096),
            },
        )

        batch_row = judgehost_fetch_batch(service, batch_id)
        self.assertIsNotNone(batch_row)
        assert batch_row is not None
        stored_compile_output = base64.b64decode(str(batch_row["compile_output_b64"] or ""))
        stored_compile_metadata = base64.b64decode(str(batch_row["compile_metadata_b64"] or ""))
        self.assertLessEqual(len(stored_compile_output), limit)
        self.assertLessEqual(len(stored_compile_metadata), limit)
        self.assertIn(b"...[truncated]", stored_compile_output)
        self.assertIn(b"...[truncated]", stored_compile_metadata)

    def test_domjudge_fetch_work_endpoint_requires_hostname(self) -> None:
        from app.main import app

        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"

        hosts_before = [str(row.get("hostname") or "") for row in service.status().get("hosts", [])]

        with TestClient(app) as client:
            resp = client.post(
                "/api/v4/judgehosts/fetch-work",
                data={"max_batchsize": "1"},
                headers={"Authorization": "Bearer test-token"},
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"detail": "hostname is required"})
        hosts_after = [str(row.get("hostname") or "") for row in service.status().get("hosts", [])]
        self.assertEqual(hosts_after, hosts_before)
        self.assertNotIn("judgehost", hosts_after)

    def test_domjudge_testcase_files_endpoint_uses_request_peer_hostname_binding(self) -> None:
        from app.main import app

        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        with TestClient(app) as client:
            verification_id = f"b-jh-peer-{uuid.uuid4().hex[:8]}"
            run_id = f"r-jh-peer-{uuid.uuid4().hex[:8]}"
            self._seed_build_verification(verification_id)
            self._seed_verification_test_artifacts(verification_id, [("001.in", "peer-input\n", "peer-output\n")])
            service.enqueue_task(
                problem=self.problem,
                username=self.user,
                artifact_verification_id=verification_id,
                mode="pass-fail",
                submission_path="solutions/ac.cpp",
                upload_content=None,
                upload_filename=None,
                run_id=run_id,
                selected_tests=["001.in"],
                verification_id=_canonical_verification_id("inv-domjudge-peer"),
                verification_run_ids=[run_id],
                expected_behavior="accepted",
                verification_source="run.execute",
            )
            fetch_resp = client.post(
                "/api/v4/judgehosts/fetch-work",
                data={"hostname": "judgehost-peer", "max_batchsize": "1"},
                headers={"Authorization": "Bearer test-token"},
            )
            self.assertEqual(fetch_resp.status_code, 200)
            tasks = fetch_resp.json()
            self.assertEqual(len(tasks), 1)
            testcase_id = int(tasks[0].get("testcase_id") or 0)
            self.assertEqual(testcase_id, int(tasks[0].get("judgetaskid") or 0))
            testcase_resp = client.get(
                f"/api/v4/judgehosts/get_files/testcase/{testcase_id}",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(testcase_resp.status_code, 200)
        files = testcase_resp.json()
        input_blob = next(item["content"] for item in files if str(item.get("filename") or "") == "input")
        self.assertEqual(base64.b64decode(input_blob).decode("utf-8", errors="replace"), "peer-input\n")

    def test_domjudge_executable_files_endpoint_uses_request_peer_hostname_binding(self) -> None:
        from app.main import app

        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        with TestClient(app) as client:
            verification_id = f"b-jh-peer-script-{uuid.uuid4().hex[:8]}"
            run_id = f"r-jh-peer-script-{uuid.uuid4().hex[:8]}"
            self._seed_build_verification(verification_id)
            service.enqueue_task(
                problem=self.problem,
                username=self.user,
                artifact_verification_id=verification_id,
                mode="pass-fail",
                submission_path="solutions/ac.cpp",
                upload_content=None,
                upload_filename=None,
                run_id=run_id,
                selected_tests=["001.in"],
                verification_id=_canonical_verification_id("inv-domjudge-peer-script"),
                verification_run_ids=[run_id],
                expected_behavior="accepted",
                verification_source="run.execute",
            )
            fetch_resp = client.post(
                "/api/v4/judgehosts/fetch-work",
                data={"hostname": "judgehost-peer-script", "max_batchsize": "1"},
                headers={"Authorization": "Bearer test-token"},
            )
            self.assertEqual(fetch_resp.status_code, 200)
            tasks = fetch_resp.json()
            self.assertEqual(len(tasks), 1)
            compare_script_id = int(tasks[0].get("compare_script_id") or 0)
            self.assertGreater(compare_script_id, 0)
            compare_resp = client.get(
                f"/api/v4/judgehosts/get_files/compare/{compare_script_id}",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(compare_resp.status_code, 200)
        files = compare_resp.json()
        filenames = {str(item.get("filename") or "") for item in files}
        self.assertIn("run", filenames)

    def test_domjudge_large_multipart_keeps_starlette_file_spool_threshold(self) -> None:
        from app.main import app

        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        old_max_part_size = int(getattr(MultiPartParser, "max_part_size", 0) or 0)
        old_max_file_size = int(getattr(MultiPartParser, "max_file_size", 0) or 0)
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        self.addCleanup(setattr, MultiPartParser, "max_part_size", old_max_part_size)
        self.addCleanup(setattr, MultiPartParser, "max_file_size", old_max_file_size)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True
        MultiPartParser.max_part_size = 1024 * 1024
        MultiPartParser.max_file_size = 1024 * 1024

        verification_id = f"b-jh-spool-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-spool-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-spool"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        service.domjudge_register_host("judgehost-spool")
        tasks = service.domjudge_fetch_work("judgehost-spool", max_batchsize=1)
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        large_output = b"A" * (20 * 1024 * 1024)
        metadata = b"cpu-time: 0.001\nwall-time: 0.001\nmemory-bytes: 4096\n"

        with TestClient(app) as client:
            headers = {"Authorization": "Bearer test-token"}
            update_resp = client.put(
                f"/api/v4/judgehosts/update-judging/judgehost-spool/{case_id}",
                data={
                    "compile_success": "1",
                    "output_compile": "",
                    "compile_metadata": "",
                },
                headers=headers,
            )
            self.assertEqual(update_resp.status_code, 200)

            add_resp = client.post(
                f"/api/v4/judgehosts/add-judging-run/judgehost-spool/{case_id}",
                data={
                    "runresult": "correct",
                    "runtime": "0.001",
                },
                files={
                    "output_run": ("program.out", large_output, "application/octet-stream"),
                    "output_diff": ("judgemessage.txt", b"ok\n", "text/plain"),
                    "metadata": ("program.meta", metadata, "text/plain"),
                },
                headers=headers,
            )
            self.assertEqual(add_resp.status_code, 200)

        self.assertEqual(int(getattr(MultiPartParser, "max_file_size", 0) or 0), 1024 * 1024)
        self.assertGreaterEqual(int(getattr(MultiPartParser, "max_part_size", 0) or 0), 20 * 1024 * 1024)

    def test_domjudge_build_solve_uses_problem_limits_when_run_config_missing(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        ws = Path(self._workspace_path())
        (ws / "config").mkdir(parents=True, exist_ok=True)
        (ws / "interactors").mkdir(parents=True, exist_ok=True)
        (ws / "config" / "problem.json").write_text(
            json.dumps({"time_limit_ms": 6000, "memory_limit_mb": 1024, "mode": "interactive"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (ws / "interactors" / "interactor.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )

        verification_id = f"b-jh-limits-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-limits-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        metadata = config.verification_service.verification_detail(verification_id)
        metadata.pop("run_config_json", None)
        config.verification_service.persist_verification_detail(verification_id, metadata)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id="",
            verification_run_ids=[],
            expected_behavior="accepted",
            verification_source="build.solve",
        )

        service.domjudge_register_host("judgehost-limits")
        tasks = service.domjudge_fetch_work("judgehost-limits", max_batchsize=1)
        self.assertEqual(len(tasks), 1)
        run_config_raw = str(tasks[0].get("run_config") or "{}")
        run_config = json.loads(run_config_raw)
        compare_config_raw = str(tasks[0].get("compare_config") or "{}")
        compare_config = json.loads(compare_config_raw)
        compile_config_raw = str(tasks[0].get("compile_config") or "{}")
        compile_config = json.loads(compile_config_raw)
        self.assertAlmostEqual(float(run_config.get("time_limit") or 0.0), 6.0, places=3)
        self.assertAlmostEqual(float(run_config.get("overshoot") or 0.0), 0.0, places=3)
        self.assertEqual(int(run_config.get("memory_limit") or 0), 1024 * 1024)
        self.assertEqual(int(run_config.get("process_limit") or 0), 1024)
        self.assertEqual(
            int(run_config.get("output_limit") or 0),
            int(getattr(service.state.constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536),
        )
        self.assertEqual(int(run_config.get("pass_limit") or 0), 1)
        compile_output_kb = int(getattr(service.state.constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 262144) or 262144)
        aux_limit_bytes = int(getattr(service.state.constants, "AUX_DISPLAY_TEXT_LIMIT_BYTES", 2048) or 2048)
        self.assertEqual(
            int(compare_config.get("script_filesize_limit") or 0),
            compile_output_kb,
        )
        self.assertEqual(
            int(compare_config.get("script_memory_limit") or 0),
            int(getattr(service.state.constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048) * 1024,
        )
        self.assertEqual(
            int(compile_config.get("script_filesize_limit") or 0),
            compile_output_kb,
        )
        self.assertGreaterEqual(int(compare_config.get("script_filesize_limit") or 0), 1024)
        self.assertGreaterEqual(int(compile_config.get("script_filesize_limit") or 0), 1024)
        self.assertNotEqual(
            int(compare_config.get("script_filesize_limit") or 0),
            (aux_limit_bytes + 1023) // 1024,
        )
        self.assertNotEqual(
            int(compile_config.get("script_filesize_limit") or 0),
            (aux_limit_bytes + 1023) // 1024,
        )

    def test_domjudge_compare_config_uses_compile_memory_when_checker_source_compiles_during_compare(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        ws = Path(self._workspace_path())
        (ws / "checkers").mkdir(parents=True, exist_ok=True)
        (ws / "checkers" / "checker.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )

        compile_mem_mb = max(
            64,
            int(getattr(service.state.constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048),
        )
        run_mem_mb = compile_mem_mb + 1024

        verification_id = f"b-jh-compare-compile-mem-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-compile-mem-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        metadata = config.verification_service.verification_detail(verification_id)
        metadata["run_config_json"] = json.dumps(
            {
                "checker_mode": "testlib",
                "checker_args": [],
                "pass_limit": 1,
                "memory_limit_mb": run_mem_mb,
            }
        )
        config.verification_service.persist_verification_detail(verification_id, metadata)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id="",
            verification_run_ids=[],
            expected_behavior="accepted",
            verification_source="build.solve",
        )

        host = "judgehost-compare-compile-memory"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(tasks), 1)

        run_config = json.loads(str(tasks[0].get("run_config") or "{}"))
        compare_config = json.loads(str(tasks[0].get("compare_config") or "{}"))
        compare_files = service.domjudge_get_executable_files(
            "compare",
            str(tasks[0].get("compare_script_id") or ""),
        )
        compare_names = {str(item.get("filename") or "") for item in compare_files}

        self.assertIn("checker.cpp", compare_names)
        self.assertEqual(int(run_config.get("memory_limit") or 0), run_mem_mb * 1024)
        self.assertGreater(int(run_config.get("memory_limit") or 0), compile_mem_mb * 1024)
        self.assertEqual(
            int(compare_config.get("script_memory_limit") or 0),
            max(int(run_config.get("memory_limit") or 0), compile_mem_mb * 1024),
        )

    def test_domjudge_main_correct_includes_checker_files_in_compare_payload(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        ws = Path(self._workspace_path())
        (ws / "checkers").mkdir(parents=True, exist_ok=True)
        (ws / "third_party" / "testlib").mkdir(parents=True, exist_ok=True)
        (ws / "checkers" / "checker.cpp").write_text(
            "#include \"testlib.h\"\nint main(int argc, char** argv){registerTestlibCmd(argc, argv); quitf(_ok, \"ok\");}\n",
            encoding="utf-8",
        )
        (ws / "third_party" / "testlib" / "testlib.h").write_text(
            "#pragma once\n#define _ok 0\ninline void registerTestlibCmd(int, char**){ }\ninline void quitf(int, const char*, ...){ }\n",
            encoding="utf-8",
        )

        verification_id = f"b-jh-main-correct-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-main-correct-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id="",
            verification_run_ids=[],
            expected_behavior="accepted",
            verification_source="main-correct",
        )

        host = "judgehost-main-correct-compare"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(tasks), 1)

        compare_files = service.domjudge_get_executable_files(
            "compare",
            str(tasks[0].get("compare_script_id") or ""),
        )
        compare_names = {str(item.get("filename") or "") for item in compare_files}

        self.assertIn("run", compare_names)
        self.assertIn("checker.cpp", compare_names)
        self.assertIn("testlib.h", compare_names)

    def test_domjudge_build_solve_defaults_pass_limit_from_problem_config(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        ws = Path(self._workspace_path())
        (ws / "config").mkdir(parents=True, exist_ok=True)
        (ws / "interactors").mkdir(parents=True, exist_ok=True)
        (ws / "config" / "problem.json").write_text(
            json.dumps({"time_limit_ms": 2000, "memory_limit_mb": 1024, "mode": "interactive", "pass_limit": 3}, indent=2) + "\n",
            encoding="utf-8",
        )
        (ws / "interactors" / "interactor.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )

        verification_id = f"b-jh-multipass-default-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-multipass-default-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        metadata = config.verification_service.verification_detail(verification_id)
        metadata.pop("run_config_json", None)
        config.verification_service.persist_verification_detail(verification_id, metadata)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id="",
            verification_run_ids=[],
            expected_behavior="accepted",
            verification_source="build.solve",
        )

        service.domjudge_register_host("judgehost-multipass-default")
        tasks = service.domjudge_fetch_work("judgehost-multipass-default", max_batchsize=1)
        self.assertEqual(len(tasks), 1)
        run_config_raw = str(tasks[0].get("run_config") or "{}")
        run_config = json.loads(run_config_raw)
        self.assertEqual(int(run_config.get("pass_limit") or 0), 3)

    def test_domjudge_active_cache_probe_finishes_hits_and_leases_only_misses(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-partial-cache-{uuid.uuid4().hex[:8]}"
        run_id_seed = f"r-jh-partial-seed-{uuid.uuid4().hex[:8]}"
        run_id_hit = f"r-jh-full-hit-{uuid.uuid4().hex[:8]}"
        run_id_target = f"r-jh-partial-target-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(
            verification_id,
            [("001.in", "ok\n", "ok\n"), ("002.in", "miss\n", "miss\n")],
        )
        service.domjudge_register_host("judgehost-partial-cache")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_seed,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-jh-partial-seed"),
            verification_run_ids=[run_id_seed],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        seed_tasks = service.domjudge_fetch_work("judgehost-partial-cache", max_batchsize=8)
        self.assertEqual(len(seed_tasks), 1)
        seed_case_id = int(seed_tasks[0].get("judgetaskid") or 0)
        self.assertGreater(seed_case_id, 0)
        service.domjudge_update_judging(
            "judgehost-partial-cache",
            seed_case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        seed_meta = "cpu-time: 0.003\nwall-time: 0.004\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-partial-cache",
            seed_case_id,
            {
                "runresult": "correct",
                "runtime": "0.003",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(seed_meta.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        hit_task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_hit,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-jh-full-hit"),
            verification_run_ids=[run_id_hit],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertEqual(service.probe_task_case_cache([hit_task_id], limit=32), set())
        self.assertEqual(service.wait_for_task(hit_task_id, timeout_sec=2.0), run_id_hit)
        hit_rows = judgehost_cases_for_run(service, run_id_hit)
        self.assertEqual([str(row["status"]) for row in hit_rows], ["reported"])

        target_task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_target,
            selected_tests=["001.in", "002.in"],
            verification_id=_canonical_verification_id("inv-jh-partial-target"),
            verification_run_ids=[run_id_target],
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        rows = judgehost_cases_for_run(service, run_id_target)
        self.assertEqual(len(rows), 2)
        self.assertEqual({str(row["status"]) for row in rows}, {"cache-pending"})

        self.assertEqual(service.probe_task_case_cache([target_task_id], limit=32), set())
        rows = judgehost_cases_for_run(service, run_id_target)
        self.assertEqual(str(rows[0]["test_name"] or ""), "001.in")
        self.assertEqual(str(rows[0]["status"] or ""), "reported")
        self.assertEqual(str(rows[1]["test_name"] or ""), "002.in")
        self.assertEqual(str(rows[1]["status"] or ""), "pending")

        leased = service.domjudge_fetch_work("judgehost-partial-cache", max_batchsize=8)
        self.assertEqual(len(leased), 1)

        rows = judgehost_cases_for_run(service, run_id_target)
        self.assertEqual(str(rows[0]["test_name"] or ""), "001.in")
        self.assertEqual(str(rows[0]["status"] or ""), "reported")
        self.assertEqual(str(rows[1]["test_name"] or ""), "002.in")
        self.assertEqual(str(rows[1]["status"] or ""), "leased")
        cached_result = service.poll_task_case_result(target_task_id, "001.in")
        self.assertIsNotNone(cached_result)
        assert cached_result is not None
        tests = list(dict(cached_result["summary"])["tests"])
        self.assertEqual(len(tests), 1)
        cached_test = tests[0] if tests else {}
        self.assertEqual(str((cached_test or {}).get("test") or ""), "001.in")
        self.assertEqual(str((cached_test or {}).get("verdict") or ""), "OK")
        self.assertEqual(int((cached_test or {}).get("time_user_ms") or 0), 3)
        self.assertTrue(str((cached_test or {}).get("output_ref") or ""))
        self.assertIsInstance((cached_test or {}).get("feedback_files"), list)
        pass_rows = (cached_test or {}).get("passes")
        self.assertIsInstance(pass_rows, list)
        self.assertEqual(str((pass_rows[0] or {}).get("verdict") or ""), "OK")
        expected_case_id = int(rows[1]["id"])
        self.assertEqual(int(leased[0].get("judgetaskid") or 0), expected_case_id)

    def test_domjudge_cache_shortcut_requires_output_blob_for_ok_result(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-cache-blob-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-cache-blob-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-cache-blob-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-cache-blob")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-cache-blob-a"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        cache_root = Path(config.settings.cache_root) / "judge-fs-index" / service.CASE_CACHE_KIND
        before_cache_outputs = {
            path.resolve()
            for path in cache_root.rglob("program.out")
            if path.is_file() and (not path.is_symlink())
        }
        tasks_a = service.domjudge_fetch_work("judgehost-cache-blob", max_batchsize=8)
        self.assertEqual(len(tasks_a), 1)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        service.domjudge_update_judging(
            "judgehost-cache-blob",
            case_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 0.002\nwall-time: 0.003\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-cache-blob",
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.002",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )
        after_cache_outputs = {
            path.resolve()
            for path in cache_root.rglob("program.out")
            if path.is_file() and (not path.is_symlink())
        }
        created_outputs = sorted(after_cache_outputs.difference(before_cache_outputs))
        target_output = Path(created_outputs[-1]) if created_outputs else None
        if target_output is None:
            self.fail("expected at least one newly created cache output blob")
        self.assertTrue(target_output.exists())
        target_output.unlink()

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-cache-blob-b"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-cache-blob", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertGreater(int(tasks_b[0].get("judgetaskid") or 0), 0)

    def test_domjudge_cache_manifest_mismatch_is_deleted_and_treated_as_miss(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-manifest-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-manifest-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-manifest-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-manifest")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-manifest-a"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_a = service.domjudge_fetch_work("judgehost-manifest", max_batchsize=8)
        self.assertEqual(len(tasks_a), 1)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        service.domjudge_update_judging(
            "judgehost-manifest",
            case_id_a,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        meta_text = "cpu-time: 0.002\nwall-time: 0.003\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-manifest",
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.002",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        cache_root = Path(config.settings.cache_root) / "judge-fs-index" / service.CASE_CACHE_KIND
        blob_paths = [p for p in cache_root.rglob("program.out") if p.is_file() and (not p.is_symlink())]
        self.assertTrue(blob_paths)
        target_blob = sorted(blob_paths)[-1]
        target_entry_dir = target_blob.parent.parent.resolve()
        markers = [
            p
            for p in target_entry_dir.iterdir()
            if p.is_file()
            and (not p.is_symlink())
            and re.fullmatch(r"[0-9a-f]{64}", str(p.name or "").strip().lower())
        ]
        self.assertTrue(markers)
        marker = markers[0]
        marker.unlink()
        (target_entry_dir / ("0" * 64)).write_bytes(b"")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-manifest-b"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-manifest", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertFalse(target_entry_dir.exists())

    def test_domjudge_cache_blob_size_mismatch_is_deleted_and_treated_as_miss(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-blobsha-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-blobsha-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-blobsha-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-blobsha")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-blobsha-a"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_a = service.domjudge_fetch_work("judgehost-blobsha", max_batchsize=8)
        self.assertEqual(len(tasks_a), 1)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        service.domjudge_update_judging(
            "judgehost-blobsha",
            case_id_a,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        meta_text = "cpu-time: 0.002\nwall-time: 0.003\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-blobsha",
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.002",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        cache_root = Path(config.settings.cache_root) / "judge-fs-index" / service.CASE_CACHE_KIND
        blob_paths = [p for p in cache_root.rglob("program.out") if p.is_file() and (not p.is_symlink())]
        self.assertTrue(blob_paths)
        target_blob = sorted(blob_paths)[-1]
        target_entry_dir = target_blob.parent.parent.resolve()
        target_blob.write_bytes(b"tampered\n")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-blobsha-b"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-blobsha", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertFalse(target_entry_dir.exists())

    def test_domjudge_expected_accepted_does_not_shortcut_non_ok_cache(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-accepted-cache-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-accepted-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-accepted-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-accepted-cache")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-accepted-cache-a"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_a = service.domjudge_fetch_work("judgehost-accepted-cache", max_batchsize=8)
        self.assertEqual(len(tasks_a), 1)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        service.domjudge_update_judging(
            "judgehost-accepted-cache",
            case_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 0.003\nwall-time: 0.004\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-accepted-cache",
            case_id_a,
            {
                "runresult": "wrong-answer",
                "runtime": "0.003",
                "output_run": base64.b64encode(b"wrong\n").decode("ascii"),
                "output_diff": base64.b64encode(b"wa\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        failed_row = self._verification_run_row(run_id_a)
        self.assertIsNotNone(failed_row)
        self.assertEqual(str(failed_row["status"] or "").strip().lower(), "ok")
        failed_summary = dict(failed_row["summary"])
        failed_tests = failed_summary.get("tests") if isinstance(failed_summary, dict) else []
        self.assertIsInstance(failed_tests, list)
        self.assertEqual(str((failed_tests[0] or {}).get("verdict") or ""), "WA")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-accepted-cache-b"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-accepted-cache", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertGreater(int(tasks_b[0].get("judgetaskid") or 0), 0)

    def test_domjudge_compare_exitcode_3_is_tagged_checker_fail(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-checker-fail-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-checker-fail-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-checker-fail")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-checker-fail"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-checker-fail", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-checker-fail",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 0.001\nwall-time: 0.001\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-checker-fail",
            case_id,
            {
                "runresult": "run-error",
                "runtime": "0.001",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"comparing failed\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": base64.b64encode(b"exitcode: 3\ncpu-time: 0.001\n").decode("ascii"),
            },
        )

        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        self.assertEqual(str(case_row["runresult"] or "").strip().lower(), "checker-fail")

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        self.assertIn("001.in: comparing failed", str(summary.get("error") or ""))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        first = tests[0] if tests else {}
        self.assertEqual(str((first or {}).get("verdict") or ""), "FL")
        self.assertEqual(str((first or {}).get("runresult") or "").strip().lower(), "checker-fail")
        passes = (first or {}).get("passes") if isinstance(first, dict) else []
        pass_row = passes[0] if isinstance(passes, list) and passes else {}
        self.assertEqual(str((pass_row or {}).get("runresult") or "").strip().lower(), "checker-fail")
        self.assertIn("comparing failed", str((pass_row or {}).get("feedback") or ""))

    def test_domjudge_run_error_prefers_program_stderr_feedback(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-run-error-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-run-error-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-run-error")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-run-error"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-run-error", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-run-error",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        stderr_text = (
            "terminate called after throwing an instance of 'std::runtime_error'\n"
            "  what(): boom\n"
        )
        meta_text = "cpu-time: 0.001\nwall-time: 0.001\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-run-error",
            case_id,
            {
                "runresult": "run-error",
                "runtime": "0.001",
                "output_run": "",
                "output_diff": base64.b64encode(b"judge fallback message\n").decode("ascii"),
                "output_error": base64.b64encode(stderr_text.encode("utf-8")).decode("ascii"),
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        summary = dict(run_row["summary"])
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        first = tests[0] if tests else {}
        self.assertEqual(str((first or {}).get("verdict") or ""), "RE")
        feedback_files = (first or {}).get("feedback_files") if isinstance(first, dict) else []
        self.assertIsInstance(feedback_files, list)
        self.assertTrue(feedback_files)
        self.assertTrue(str(feedback_files[0] or "").endswith("program.err"))
        passes = (first or {}).get("passes") if isinstance(first, dict) else []
        pass_row = passes[0] if isinstance(passes, list) and passes else {}
        self.assertIn("terminate called after throwing", str((pass_row or {}).get("feedback") or ""))
        self.assertNotIn("judge fallback message", str((pass_row or {}).get("feedback") or ""))

    def test_domjudge_compare_exitcode_negative_with_hard_tl_is_tl(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compare-neg-tl-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-neg-tl-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-compare-neg-tl")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-compare-neg-tl"),
            verification_run_ids=[run_id],
            expected_behavior="wrong-answer-or-time-limit",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-neg-tl", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compare-neg-tl",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = (
            "cpu-time: 18.000\n"
            "wall-time: 36.200\n"
            "memory-bytes: 76562432\n"
            "signal: 14\n"
            "time-result: hard-timelimit\n"
            "stdout-bytes: 1076310313\n"
        )
        service.domjudge_add_judging_run(
            "judgehost-compare-neg-tl",
            case_id,
            {
                "runresult": "compare-error",
                "runtime": "36.200",
                "output_run": "",
                "output_diff": "",
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": base64.b64encode(b"exitcode: -1\n").decode("ascii"),
            },
        )

        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        self.assertEqual(str(case_row["runresult"] or "").strip().lower(), "timelimit")

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "ok")
        summary = dict(run_row["summary"])
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        first = tests[0] if tests else {}
        self.assertEqual(str((first or {}).get("verdict") or ""), "TL")
        self.assertEqual(str((first or {}).get("runresult") or "").strip().lower(), "timelimit")

    def test_domjudge_compare_script_internal_error_fails_whole_run(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compare-internal-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-internal-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-compare-internal")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-compare-internal"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-internal", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compare-internal",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        service.domjudge_internal_error(
            description="compare script 173 crashed with exit code 3, expected one of 42/43",
            judgetask_id=case_id,
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        self.assertIn(
            "compare script 173 crashed with exit code 3, expected one of 42/43",
            str(summary.get("error") or ""),
        )

    def test_domjudge_internal_error_includes_debug_fail_message(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compare-debug-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-debug-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-compare-debug")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-compare-debug"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-debug", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compare-debug",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        service.domjudge_add_debug_info(
            hostname="judgehost-compare-debug",
            judgetask_id=case_id,
            payload={"message": "FAIL Can not write to the result file (test case 1)"},
        )
        service.domjudge_internal_error(
            description="compare script 33 crashed with exit code 3, expected one of 42/43",
            judgetask_id=case_id,
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_internal_error_includes_payload_fail_message(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compare-payload-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-payload-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-compare-payload")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-compare-payload"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-payload", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compare-payload",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        service.domjudge_internal_error(
            description="compare script 33 crashed with exit code 3, expected one of 42/43",
            judgetask_id=case_id,
            payload={"message": "FAIL Can not write to the result file (test case 1)"},
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_internal_error_includes_job_level_debug_message(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compare-batch-debug-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-batch-debug-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(
            verification_id,
            [("001.in", "ok\n", "ok\n"), ("002.in", "ok\n", "ok\n")],
        )
        service.domjudge_register_host("judgehost-compare-batch-debug")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in", "002.in"],
            verification_id=_canonical_verification_id("inv-compare-batch-debug"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-batch-debug", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        service.domjudge_update_judging(
            "judgehost-compare-batch-debug",
            int(leased[0]["judgetaskid"]),
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        leased += service.domjudge_fetch_work("judgehost-compare-batch-debug", max_batchsize=8)
        self.assertEqual(len(leased), 2)
        case_ids = sorted(int(item.get("judgetaskid") or 0) for item in leased)
        self.assertGreater(case_ids[0], 0)
        self.assertGreater(case_ids[1], 0)
        target_case = case_ids[1]
        row = judgehost_fetch_case(service, target_case)
        self.assertIsNotNone(row)
        batch_id = int(row["batch_id"])

        service.domjudge_add_debug_info(
            hostname="judgehost-compare-batch-debug",
            judgetask_id=batch_id,
            payload={"message": "FAIL compare script output from batch-level debug"},
        )
        service.domjudge_internal_error(
            description="compare script 33 crashed with exit code 3, expected one of 42/43",
            judgetask_id=target_case,
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("FAIL compare script output from batch-level debug", error_text)

    def test_domjudge_internal_error_includes_judgehostlog_compare_output(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compare-jhlog-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-jhlog-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-compare-jhlog")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-compare-jhlog"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-jhlog", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compare-jhlog",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        judgehost_log = (
            "testcase_run.sh: Comparing failed with exitcode 3, compare script output:\\n"
            "FAIL Can not write to the result file (test case 1)\\n"
        )
        service.domjudge_internal_error(
            description="compare script 33 crashed with exit code 3, expected one of 42/43",
            judgetask_id=case_id,
            payload={"judgehostlog": base64.b64encode(judgehost_log.encode("utf-8")).decode("ascii")},
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("Comparing failed with exitcode 3, compare script output:", error_text)
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_internal_error_strips_raw_base64_payload_blob(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-compare-strip-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-strip-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-compare-strip")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-compare-strip"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-strip", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compare-strip",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        description = "compare script 33 crashed with exit code 3, expected one of 42/43"
        judgehost_log = (
            "[Mar 22 20:27:50.752] testcase_run.sh[18759]: Comparing failed with exitcode 3, compare script output:\n"
            "Expected integer, but \"\"$SUBMISSION_BIN\"\" found (test case 1, testdata.in)\n"
        )
        judgehost_log_b64 = base64.b64encode(judgehost_log.encode("utf-8")).decode("ascii")
        service.domjudge_internal_error(
            description=description,
            judgetask_id=case_id,
            payload={
                "description": description,
                "judgehostlog": judgehost_log_b64,
                "disabled": "{\"kind\":\"compare_script\",\"compare_script_id\":\"33\"}",
                "hostname": "judgedaemon-2-2",
                "judgetaskid": str(case_id),
            },
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        error_text = str(summary.get("error") or "")
        self.assertIn(description, error_text)
        self.assertIn("Comparing failed with exitcode 3, compare script output:", error_text)
        self.assertIn("Expected integer, but \"\"$SUBMISSION_BIN\"\" found", error_text)
        self.assertNotIn(judgehost_log_b64, error_text)
        self.assertNotIn("\"judgehostlog\":", error_text)
        self.assertNotIn("\"disabled\":", error_text)

    def test_domjudge_fl_result_is_persisted_but_never_shortcut_reused(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-fl-cache-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-fl-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-fl-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-fl-cache")

        before_count = self._judge_index_entry_count(service.CASE_CACHE_KIND)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-fl-cache-a"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_a = service.domjudge_fetch_work("judgehost-fl-cache", max_batchsize=8)
        self.assertEqual(len(tasks_a), 1)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)

        service.domjudge_update_judging(
            "judgehost-fl-cache",
            case_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-fl-cache",
            case_id_a,
            {
                "runresult": "internal-error",
                "runtime": "0.004",
                "output_run": base64.b64encode(b"").decode("ascii"),
                "output_diff": base64.b64encode(b"judge failed\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )
        failed_row = self._verification_run_row(run_id_a)
        self.assertIsNotNone(failed_row)
        self.assertEqual(str(failed_row["status"] or ""), "failed")
        failed_summary = dict(failed_row["summary"])
        failed_tests = failed_summary.get("tests") if isinstance(failed_summary, dict) else []
        self.assertIsInstance(failed_tests, list)
        self.assertEqual(str((failed_tests[0] or {}).get("verdict") or ""), "FL")
        self.assertIn("001.in", str(failed_summary.get("error") or ""))
        self.assertIn("judge failed", str(failed_summary.get("error") or "").lower())

        after_fl_count = self._judge_index_entry_count(service.CASE_CACHE_KIND)
        self.assertGreater(after_fl_count, before_count)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-fl-cache-b"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-fl-cache", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_a, case_id_b)

    def test_domjudge_bypass_case_result_cache_bypasses_case_cache(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-recompile-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-recompile-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-recompile-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-recompile-a"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        service.domjudge_register_host("judgehost-recompile")
        tasks_a = service.domjudge_fetch_work("judgehost-recompile", max_batchsize=8)
        self.assertEqual(len(tasks_a), 1)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        service.domjudge_update_judging(
            "judgehost-recompile",
            case_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 0.002\nwall-time: 0.003\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-recompile",
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.002",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )
        finished_a = self._verification_run_row(run_id_a)
        self.assertIsNotNone(finished_a)
        self.assertEqual(str(finished_a["status"] or ""), "ok")

        with patch.object(service._toolkit, "cache_delete", wraps=service._toolkit.cache_delete) as cache_delete:
            service.enqueue_task(
                problem=self.problem,
                username=self.user,
                artifact_verification_id=verification_id,
                mode="pass-fail",
                submission_path="solutions/ac.cpp",
                upload_content=None,
                upload_filename=None,
                run_id=run_id_b,
                selected_tests=["001.in"],
                verification_id=_canonical_verification_id("inv-recompile-b"),
                verification_run_ids=[run_id_b],
                expected_behavior="accepted",
                verification_source="run.execute",
                bypass_case_result_cache=True,
            )
            tasks_b = service.domjudge_fetch_work("judgehost-recompile", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertGreaterEqual(cache_delete.call_count, 1)
        deleted_refs = {call.args for call in cache_delete.call_args_list}
        self.assertEqual(len(deleted_refs), 1)
        self.assertEqual(next(iter(deleted_refs))[0], service.CASE_CACHE_KIND)
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_a, case_id_b)

    def test_domjudge_compiled_job_pending_cases_can_be_shared_across_hosts(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-share-hosts-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-share-hosts-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(
            verification_id,
            [("001.in", "ok\n", "ok\n"), ("002.in", "ok-2\n", "ok-2\n")],
        )

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in", "002.in"],
            verification_id=_canonical_verification_id("inv-share-hosts"),
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertTrue(task_id.startswith("jt-"))
        service.domjudge_register_host("judgehost-share-a")
        service.domjudge_register_host("judgehost-share-b")

        tasks_a = service.domjudge_fetch_work("judgehost-share-a", max_batchsize=1)
        self.assertEqual(len(tasks_a), 1)
        protocol_job_id = int(tasks_a[0].get("jobid") or 0)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        case_a = judgehost_fetch_case(service, case_id_a)
        assert case_a is not None
        internal_batch_id = int(case_a["batch_id"])
        self.assertGreaterEqual(protocol_job_id, 0)
        self.assertGreater(case_id_a, 0)

        service.domjudge_update_judging(
            "judgehost-share-a",
            case_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )

        tasks_b = service.domjudge_fetch_work("judgehost-share-b", max_batchsize=1)
        self.assertEqual(len(tasks_b), 1)
        self.assertEqual(int(tasks_b[0].get("jobid") or 0), protocol_job_id)
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_b, case_id_a)
        case_b = judgehost_fetch_case(service, case_id_b)
        assert case_b is not None
        self.assertEqual(int(case_b["batch_id"]), internal_batch_id)

        case_row_a = judgehost_fetch_case(service, case_id_a)
        case_row_b = judgehost_fetch_case(service, case_id_b)
        self.assertIsNotNone(case_row_a)
        self.assertIsNotNone(case_row_b)
        self.assertEqual(str(case_row_a["lease_owner"] or ""), "judgehost-share-a")
        self.assertEqual(str(case_row_b["lease_owner"] or ""), "judgehost-share-b")

    def test_domjudge_fetch_work_defers_preemption_until_inflight_case_reports(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-preempt-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        self._seed_verification_test_artifacts(
            verification_id,
            [("001.in", "ok\n", "ok\n"), ("002.in", "ok-2\n", "ok-2\n")],
        )

        low_task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=f"r-jh-preempt-low-{uuid.uuid4().hex[:8]}",
            selected_tests=["001.in", "002.in"],
            verification_id=_canonical_verification_id("inv-jh-preempt-low"),
            verification_run_ids=[],
            expected_behavior="accepted",
            verification_source="run.execute",
            task_kind="solution-run",
        )
        host = "judgehost-priority-preempt"
        service.domjudge_register_host(host)
        first_tasks = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(first_tasks), 1)
        self.assertEqual(len(self._work_rows_for_task(service, first_tasks, low_task_id)), 1)

        high_task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"#include <bits/stdc++.h>\nint main(){return 0;}\n",
            upload_filename="gen.cpp",
            run_id=f"r-jh-preempt-high-{uuid.uuid4().hex[:8]}",
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-jh-preempt-high"),
            verification_run_ids=[],
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            service_class="foreground",
        )
        second_tasks = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(second_tasks, [])
        first_case_id = int(first_tasks[0].get("judgetaskid") or 0)
        first_case = judgehost_fetch_case(service, first_case_id)
        self.assertIsNotNone(first_case)
        self.assertEqual(str(first_case["status"] or ""), "leased")
        self.assertEqual(str(first_case["lease_owner"] or ""), host)

        meta_text = "cpu-time: 0.001\nwall-time: 0.001\nmemory-bytes: 4096\n"
        service.domjudge_update_judging(
            host,
            first_case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        service.domjudge_add_judging_run(
            host,
            first_case_id,
            {
                "runresult": "correct",
                "runtime": "0.001",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )
        reported_case = judgehost_fetch_case(service, first_case_id)
        self.assertIsNotNone(reported_case)
        self.assertEqual(str(reported_case["status"] or ""), "reported")

        second_tasks = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(second_tasks), 1)
        self.assertEqual(len(self._work_rows_for_task(service, second_tasks, high_task_id)), 1)

    def test_poll_task_case_result_reports_missing_shared_case_explicitly(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        verification_id = f"ver-{uuid.uuid4().hex}"
        run_id = f"r-jh-missing-case-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=verification_id,
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
            persist_verification_run=False,
        )
        service.state.task_registry.transition(
            task_id,
            expected={service.STATUS_QUEUED},
            status=service.STATUS_FAILED,
            updates={
                "run_status": "failed",
                "error_text": "",
                "summary": {
                "mode": "pass-fail",
                "source": "solutions/ac.cpp",
                "tests": [],
                "compile_diagnostics": [],
                "error": "",
                },
            },
        )

        result = service.poll_task_case_result(task_id, "001.in")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(str(result.get("status") or ""), "failed")
        self.assertTrue(bool(result.get("missing_case_result")))
        self.assertIn("missing for 001.in", str(result.get("error") or ""))
        summary = dict(result.get("summary") or {})
        tests = list(summary.get("tests") or [])
        self.assertEqual(tests, [])

    def test_poll_task_case_result_uses_canonical_case_feedback(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        verification_id = f"ver-{uuid.uuid4().hex}"
        run_id = f"r-jh-case-feedback-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = f"jt-feedback-{uuid.uuid4().hex[:8]}"
        self._insert_task_receipt(
            service,
            task_id=task_id,
            run_id=run_id,
            verification_id=verification_id,
        )

        service.state.task_registry.update(
            task_id,
            {"summary": {
                "mode": "pass-fail",
                "source": "solutions/std.cpp",
                "tests": [],
                "compile_diagnostics": [],
                "error": "",
            }},
        )

        work_root = Path(tempfile.mkdtemp(prefix="polygon-case-feedback-")).resolve()
        self.addCleanup(shutil.rmtree, work_root, ignore_errors=True)
        feedback_text = "Unexpected character #10, but ' ' expected (testdata.in)"

        batch_id = service.state.batch_scheduler.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            execution_signature="b" * 64,
            verification_id=verification_id,
            compile_key="8" * 64,
            compile_submission=self._compile_submission("std.cpp", "8" * 64),
            contest_id="",
            mode="pass-fail",
            source_name="std.cpp",
            source_path="solutions/std.cpp",
            work_root=str(work_root),
            compile_hash="a" * 32,
            run_hash="b" * 32,
            compare_hash="c" * 32,
            source_hash="d" * 64,
            compile_config_json="{}",
            run_config_json="{}",
            compare_config_json="{}",
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=0,
            service_class="background",
            batch_spec=ExecutionBatchSpec(),
            created_at="2026-04-14T00:00:00+00:00",
            case_rows=[
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "test_name": "016.in",
                    "ordinal": 1,
                    "testcase_id": 16,
                    "testcase_hash": "e" * 64,
                    "testcase_input_hash": "f" * 64,
                    "testcase_answer_hash": "0" * 64,
                    "input_ref": "",
                    "answer_ref": "",
                    "status": "pending",
                }
            ],
        )
        self.assertGreater(batch_id, 0)
        case_id = self._lease_only_case(service, batch_id, "judgehost-feedback")
        self._commit_case_result(
            service,
            case_id=case_id,
            hostname="judgehost-feedback",
            test_name="016.in",
            runresult="checker-fail",
            verdict="FL",
            feedback_text=feedback_text,
            feedback_files=["feedback/judgemessage.txt"],
        )

        result = service.poll_task_case_result(task_id, "016.in")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(str(result.get("status") or ""), "failed")
        self.assertEqual(str(result.get("error") or ""), feedback_text)
        summary = dict(result.get("summary") or {})
        self.assertEqual(str(summary.get("error") or ""), feedback_text)
        tests = list(summary.get("tests") or [])
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("message") or ""), feedback_text)
        self.assertEqual(list(tests[0].get("feedback_files") or []), ["feedback/judgemessage.txt"])

    def test_domjudge_add_debug_info_overwrites_terminal_verification_task_detail(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        verification_id = f"ver-{uuid.uuid4().hex}"
        run_id = f"r-jh-late-debug-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)

        task_id = f"jt-late-debug-{uuid.uuid4().hex[:8]}"
        self._insert_task_receipt(
            service,
            task_id=task_id,
            run_id=run_id,
            verification_id=verification_id,
        )
        service.state.task_registry.update(
            task_id,
            {"summary": {
                "mode": "pass-fail",
                "source": "solutions/std.cpp",
                "tests": [],
                "compile_diagnostics": [],
                "error": "",
            }},
        )
        task_store = config.verification_task_store
        task_store.replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-late-debug",
                    "task_kind": "main-correct",
                    "source_path": "solutions/ac.cpp",
                    "logical_run_id": run_id,
                    "test_name": "016.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                }
            ],
            edges=[],
        )
        task_store.set_task_queued("vt-late-debug", run_id=run_id, judgehost_task_id=task_id)

        work_root = Path(tempfile.mkdtemp(prefix="polygon-late-debug-")).resolve()
        self.addCleanup(shutil.rmtree, work_root, ignore_errors=True)
        batch_id = service.state.batch_scheduler.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            execution_signature="c" * 64,
            verification_id=verification_id,
            compile_key="9" * 64,
            compile_submission=self._compile_submission("std.cpp", "9" * 64),
            contest_id="",
            mode="pass-fail",
            source_name="std.cpp",
            source_path="solutions/std.cpp",
            work_root=str(work_root),
            compile_hash="a" * 32,
            run_hash="b" * 32,
            compare_hash="c" * 32,
            source_hash="d" * 64,
            compile_config_json="{}",
            run_config_json="{}",
            compare_config_json="{}",
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=0,
            service_class="background",
            batch_spec=ExecutionBatchSpec(),
            created_at="2026-04-14T00:00:00+00:00",
            case_rows=[
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "test_name": "016.in",
                    "ordinal": 1,
                    "testcase_id": 16,
                    "testcase_hash": "e" * 64,
                    "testcase_input_hash": "f" * 64,
                    "testcase_answer_hash": "0" * 64,
                    "input_ref": "",
                    "answer_ref": "",
                    "status": "pending",
                }
            ],
        )
        self.assertGreater(batch_id, 0)
        case_id = self._lease_only_case(service, batch_id, "judgehost-late-debug")
        self._commit_case_result(
            service,
            case_id=case_id,
            hostname="judgehost-late-debug",
            test_name="016.in",
            runresult="checker-fail",
            verdict="FL",
        )

        service.result._domjudge_finalize_batch_if_ready(batch_id)
        before = next(
            row for row in task_store.list_rows(verification_id)
            if str(row["task_kind"]) == "main-correct" and str(row["test_name"]) == "016.in"
        )
        self.assertEqual(str(before["error_text"] or ""), "main correct failed on 016.in")

        feedback_text = "Unexpected character #10, but ' ' expected (testdata.in)"
        service.domjudge_add_debug_info(
            hostname="judgehost-late-debug",
            judgetask_id=case_id,
            payload={"message": feedback_text},
        )

        after = next(
            row for row in task_store.list_rows(verification_id)
            if str(row["task_kind"]) == "main-correct" and str(row["test_name"]) == "016.in"
        )
        self.assertEqual(str(after["error_text"] or ""), feedback_text)
        self.assertEqual(str(after["feedback_text"] or ""), feedback_text)
        verification_row = db_fetch_one(
            "SELECT fail_reason FROM verifications WHERE id=?",
            [verification_id],
        )
        self.assertIsNotNone(verification_row)
        assert verification_row is not None
        self.assertIn(feedback_text, str(verification_row["fail_reason"] or ""))

    def test_domjudge_groups_distinct_generate_input_tasks_in_one_job(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-shared-generate-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        generator_source = b"#include <iostream>\nint main(){ std::cout << \"ok\\n\"; return 0; }\n"
        validator_source = (
            "#include \"testlib.h\"\n"
            "int main(){\n"
            "  registerValidation();\n"
            "  inf.readToken();\n"
            "  inf.readEof();\n"
            "  return 0;\n"
            "}\n"
        ).encode("utf-8")
        extra_testlib = base64.b64encode(b"").decode("ascii")
        payload_base = {
            "run_config_json": json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "pass_limit": 1,
                    "time_limit_ms": 30000,
                    "memory_limit_mb": 1024,
                },
                separators=(",", ":"),
            ),
            "problem_limits": {
                "time_limit_ms": 30000,
                "memory_limit_mb": 1024,
                "pass_limit": 1,
            },
            "binaries_b64": {},
            "sources_b64": {
                "validator.cpp": base64.b64encode(validator_source).decode("ascii"),
                "testlib.h": extra_testlib,
            },
        }
        plan_a = VerificationTestPlan(
            test_name="001.in",
            source_kind="gen",
            display_source_path="generators/gen.cpp",
            execution_source_name="gen.cpp",
            execution_source_bytes=generator_source,
            execution_input_bytes=b"\"$SUBMISSION_BIN\" 1\n",
            extra_sources_b64={"testlib.h": extra_testlib},
            tests_meta={},
            sample=False,
            sample_input_custom=False,
            sample_input_text="",
            uses_custom_sample_input=False,
            sample_output_text="",
            sample_output_validate=True,
        )
        plan_b = VerificationTestPlan(
            test_name="002.in",
            source_kind="gen",
            display_source_path="generators/gen.cpp",
            execution_source_name="gen.cpp",
            execution_source_bytes=generator_source,
            execution_input_bytes=b"\"$SUBMISSION_BIN\" 2\n",
            extra_sources_b64={"testlib.h": extra_testlib},
            tests_meta={},
            sample=False,
            sample_input_custom=False,
            sample_input_text="",
            uses_custom_sample_input=False,
            sample_output_text="",
            sample_output_validate=True,
        )
        run_id_a = f"r-jh-grouped-generate-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-grouped-generate-b-{uuid.uuid4().hex[:8]}"
        self.assertNotEqual(run_id_a, run_id_b)

        prepared_a = _prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_a,
            test_name="001.in",
            input_bytes=plan_a.execution_input_bytes,
            answer_bytes=b"",
            verification_payload_base=payload_base,
            extra_sources_b64=plan_a.extra_sources_b64,
        )
        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=generator_source,
            upload_filename="gen.cpp",
            run_id=run_id_a,
            selected_tests=[],
            verification_id=_canonical_verification_id("shared-generate"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            persist_verification_run=False,
            prepared_payload=prepared_a,
        )
        self.assertTrue(task_id.startswith("jt-"))
        service.domjudge_register_host("judgehost-shared-generate")
        tasks_a = service.domjudge_fetch_work("judgehost-shared-generate", max_batchsize=8)
        self.assertEqual(len(tasks_a), 1)
        protocol_job_id = int(tasks_a[0].get("jobid") or 0)
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        case_a = judgehost_fetch_case(service, case_id_a)
        assert case_a is not None
        internal_batch_id = int(case_a["batch_id"])
        self.assertGreaterEqual(protocol_job_id, 0)
        self.assertGreater(case_id_a, 0)

        service.domjudge_update_judging(
            "judgehost-shared-generate",
            case_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )

        prepared_b = _prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_b,
            test_name="002.in",
            input_bytes=plan_b.execution_input_bytes,
            answer_bytes=b"",
            verification_payload_base=payload_base,
            extra_sources_b64=plan_b.extra_sources_b64,
        )
        second_task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=generator_source,
            upload_filename="gen.cpp",
            run_id=run_id_b,
            selected_tests=[],
            verification_id=_canonical_verification_id("shared-generate"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            persist_verification_run=False,
            prepared_payload=prepared_b,
        )
        self.assertNotEqual(second_task_id, task_id)

        tasks_b = service.domjudge_fetch_work("judgehost-shared-generate", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertEqual(int(tasks_b[0].get("jobid") or 0), protocol_job_id)
        self.assertRegex(str(tasks_a[0].get("uuid") or ""), r"^[0-9a-f]{64}$")
        self.assertEqual(tasks_b[0]["uuid"], tasks_a[0]["uuid"])
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_b, case_id_a)
        case_b = judgehost_fetch_case(service, case_id_b)
        assert case_b is not None
        self.assertEqual(int(case_b["batch_id"]), internal_batch_id)

        meta_text = "cpu-time: 0.002\nwall-time: 0.003\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-shared-generate",
            case_id_a,
            {
                "runresult": "correct",
                "runtime": "0.002",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )
        service.domjudge_add_judging_run(
            "judgehost-shared-generate",
            case_id_b,
            {
                "runresult": "correct",
                "runtime": "0.002",
                "output_run": base64.b64encode(b"ok\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        case_rows = service.state.batch_scheduler.cases_for_batch(internal_batch_id)
        self.assertEqual([str(row["test_name"] or "") for row in case_rows], ["001.in", "002.in"])
        self.assertEqual([str(row["task_id"] or "") for row in case_rows], [task_id, second_task_id])
        self.assertEqual(
            [str(row["run_id"] or "") for row in case_rows],
            [run_id_a, run_id_b],
        )

    def test_domjudge_grouped_batch_uses_stable_uuid_across_fetches(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service.state.enabled
        old_token = service.state.api_token
        old_username = service.state.api_username
        old_include_build_payload = service.state.include_build_payload
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        self.addCleanup(setattr, service.state, "api_username", old_username)
        self.addCleanup(setattr, service.state, "include_build_payload", old_include_build_payload)
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True

        verification_id = f"b-jh-grouped-batch-one-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        generator_source = b"#include <iostream>\nint main(){ std::cout << \"ok\\n\"; return 0; }\n"
        validator_source = (
            "#include \"testlib.h\"\n"
            "int main(){\n"
            "  registerValidation();\n"
            "  inf.readToken();\n"
            "  inf.readEof();\n"
            "  return 0;\n"
            "}\n"
        ).encode("utf-8")
        extra_testlib = base64.b64encode(b"").decode("ascii")
        payload_base = {
            "run_config_json": json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "pass_limit": 1,
                    "time_limit_ms": 30000,
                    "memory_limit_mb": 1024,
                },
                separators=(",", ":"),
            ),
            "problem_limits": {
                "time_limit_ms": 30000,
                "memory_limit_mb": 1024,
                "pass_limit": 1,
            },
            "binaries_b64": {},
            "sources_b64": {
                "validator.cpp": base64.b64encode(validator_source).decode("ascii"),
                "testlib.h": extra_testlib,
            },
        }
        run_id_a = f"r-jh-grouped-batch-one-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-grouped-batch-one-b-{uuid.uuid4().hex[:8]}"
        prepared_a = _prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_a,
            test_name="003.in",
            input_bytes=b"\"$SUBMISSION_BIN\" 3\n",
            answer_bytes=b"",
            verification_payload_base=payload_base,
            extra_sources_b64={"testlib.h": extra_testlib},
        )
        prepared_b = _prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_b,
            test_name="004.in",
            input_bytes=b"\"$SUBMISSION_BIN\" 4\n",
            answer_bytes=b"",
            verification_payload_base=payload_base,
            extra_sources_b64={"testlib.h": extra_testlib},
        )
        task_id_a = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=generator_source,
            upload_filename="gen.cpp",
            run_id=run_id_a,
            selected_tests=[],
            verification_id=_canonical_verification_id("grouped-batch-one"),
            verification_run_ids=[run_id_a],
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            persist_verification_run=False,
            prepared_payload=prepared_a,
        )
        task_id_b = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=generator_source,
            upload_filename="gen.cpp",
            run_id=run_id_b,
            selected_tests=[],
            verification_id=_canonical_verification_id("grouped-batch-one"),
            verification_run_ids=[run_id_b],
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            persist_verification_run=False,
            prepared_payload=prepared_b,
        )
        self.assertNotEqual(task_id_a, task_id_b)

        service.domjudge_register_host("judgehost-grouped-batch-one")
        tasks_a = service.domjudge_fetch_work("judgehost-grouped-batch-one", max_batchsize=1)
        self.assertEqual(len(tasks_a), 1)
        self.assertRegex(str(tasks_a[0].get("uuid") or ""), r"^[0-9a-f]{64}$")
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        self.assertEqual(int(tasks_a[0].get("testcase_id") or 0), case_id_a)
        case_row_a = judgehost_fetch_case(service, case_id_a)
        self.assertIsNotNone(case_row_a)
        self.assertEqual(int(case_row_a["testcase_id"] or 0), 3)
        batch_id = int(tasks_a[0].get("jobid") or 0)
        self.assertGreater(batch_id, 0)

        service.domjudge_update_judging(
            "judgehost-grouped-batch-one",
            case_id_a,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )

        tasks_b = service.domjudge_fetch_work("judgehost-grouped-batch-one", max_batchsize=1)
        self.assertEqual(len(tasks_b), 1)
        self.assertEqual(int(tasks_b[0].get("jobid") or 0), batch_id)
        self.assertEqual(tasks_b[0]["uuid"], tasks_a[0]["uuid"])
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertEqual(int(tasks_b[0].get("testcase_id") or 0), case_id_b)
        case_row_b = judgehost_fetch_case(service, case_id_b)
        self.assertIsNotNone(case_row_b)
        self.assertEqual(int(case_row_b["testcase_id"] or 0), 4)
        self.assertNotEqual(
            case_id_b,
            case_id_a,
        )
