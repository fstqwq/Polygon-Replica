from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

from app.service.platform.hashing import domjudge_executable_hash
from app.service.runtime.invocation_backend import JudgehostDomserverInvocationBackend
from tests.common import SmokeBase, config


class TestJudgehostService(SmokeBase):
    def _build_artifact_root(self, build_id: str) -> Path:
        row = config.db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [str(build_id or "").strip()])
        if row is None:
            raise AssertionError(f"missing build row: {build_id}")
        build_ref = str(row["build_ref"] or "").strip().lower()
        if not build_ref:
            raise AssertionError(f"missing build_ref for build: {build_id}")
        return config.fs_manager.build_paths(build_ref).root.resolve()

    def _seed_build(self, build_id: str) -> None:
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
        build_ref = config.fs_manager.compute_build_ref({"suite": "judgehost", "problem": self.problem, "build_id": build_id})
        artifact_root = config.fs_manager.ensure_build_layout(build_ref).root.resolve()
        (artifact_root / "tests" / "001.in").write_text("ok\n", encoding="utf-8")
        (artifact_root / "ans" / "001.ans").write_text("ok\n", encoding="utf-8")
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps({"checker_mode": "testlib", "checker_args": [], "max_passes": 1}, indent=2) + "\n",
            encoding="utf-8",
        )
        config.db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-28T00:00:00Z",
                "2026-02-28T00:00:00Z",
            ],
        )

    def _judge_index_entry_count(self, kind: str) -> int:
        return int(config.judge_fs_index_service.count_entries(kind=kind))

    @staticmethod
    def _state_lock_owned(service) -> bool:
        checker = getattr(service._state_lock, "_is_owned", None)
        if callable(checker):
            return bool(checker())
        return False

    @staticmethod
    def _reset_task_queue_state(service) -> None:
        with service._state_lock:
            service._tasks_by_id.clear()
            service._task_id_by_run.clear()

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

    def test_judgehost_task_lifecycle_updates_run(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service._enabled
        old_token = service._api_token
        old_include_build_payload = service._include_build_payload
        self.addCleanup(setattr, service, "_enabled", old_enabled)
        self.addCleanup(setattr, service, "_api_token", old_token)
        self.addCleanup(setattr, service, "_include_build_payload", old_include_build_payload)
        service._enabled = True
        service._api_token = "test-token"
        service._include_build_payload = True

        build_id = f"b-jh-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-jh",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        self.assertTrue(task_id.startswith("jt-"))

        tasks = service.fetch_work("judgehost-1", limit=16)
        claimed = next((row for row in tasks if str(row.get("run_id") or "") == run_id), None)
        self.assertIsNotNone(claimed)
        self.assertEqual(str(claimed.get("task_id") or ""), task_id)
        self.assertEqual(str(claimed.get("run_id") or ""), run_id)

        result = service.report_result(
            task_id=task_id,
            hostname="judgehost-1",
            payload={
                "run_status": "ok",
                "summary": {
                    "mode": "pass-fail",
                    "source": "solutions/ac.cpp",
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
                    "limits": {},
                    "usage": {},
                },
            },
        )
        self.assertEqual(str(result.get("run_id") or ""), run_id)
        self.assertEqual(str(result.get("status") or ""), "ok")
        self.assertEqual(service.wait_for_task(task_id, timeout_sec=2.0), run_id)
        status = service.status()
        hosts = status.get("hosts") if isinstance(status, dict) else []
        self.assertIsInstance(hosts, list)
        self.assertGreaterEqual(int(status.get("hosts_total") or 0), 1)
        self.assertTrue(any(str(item.get("hostname") or "") == "judgehost-1" for item in hosts))

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        invocation = summary.get("invocation") if isinstance(summary, dict) else {}
        self.assertIsInstance(invocation, dict)
        self.assertTrue(bool(invocation.get("completed")))

    def test_enqueue_task_does_not_hold_state_lock_during_run_row_ensure(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        build_id = f"b-jh-lock-enqueue-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-lock-enqueue-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        original_ensure = service._ensure_run_row
        observed = {"called": 0}

        def wrapped_ensure(*args, **kwargs):
            observed["called"] += 1
            self.assertFalse(self._state_lock_owned(service))
            return original_ensure(*args, **kwargs)

        with patch.object(service, "_ensure_run_row", side_effect=wrapped_ensure):
            task_id = service.enqueue_task(
                problem=self.problem,
                username=self.user,
                build_id=build_id,
                mode="pass-fail",
                submission_path="solutions/ac.cpp",
                upload_content=None,
                upload_filename=None,
                run_id=run_id,
                selected_tests=["001.in"],
                invocation_id="inv-lock-enqueue",
                invocation_run_ids=[run_id],
                expected_behavior="accepted",
                invocation_source="run.execute",
            )
        self.assertTrue(task_id.startswith("jt-"))
        self.assertEqual(observed["called"], 1)
        row = service._task_by_id(task_id)
        self.assertIsNotNone(row)
        self.assertEqual(str(row.get("status") or ""), service.STATUS_QUEUED)

    def test_fetch_work_calls_requeue_without_state_lock(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        build_id = f"b-jh-lock-fetch-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-lock-fetch-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-lock-fetch",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        original_requeue = service._requeue_expired_leases
        observed = {"called": 0}

        def wrapped_requeue(*args, **kwargs):
            observed["called"] += 1
            self.assertFalse(self._state_lock_owned(service))
            return original_requeue(*args, **kwargs)

        with patch.object(service, "_requeue_expired_leases", side_effect=wrapped_requeue):
            rows = service.fetch_work("judgehost-lock-fetch", limit=1)
        self.assertEqual(observed["called"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("run_id") or ""), run_id)

    def test_report_result_db_io_runs_without_state_lock(self) -> None:
        service = config.judgehost_task_service
        self._reset_task_queue_state(service)
        build_id = f"b-jh-lock-report-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-lock-report-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-lock-report",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        fetched = service.fetch_work("judgehost-lock-report", limit=1)
        self.assertEqual(len(fetched), 1)
        self.assertEqual(str(fetched[0].get("task_id") or ""), task_id)

        original_load_summary = service._load_run_summary
        original_db_execute = service._db_execute
        observed = {"load": 0, "update_runs": 0}

        def wrapped_load_summary(*args, **kwargs):
            observed["load"] += 1
            self.assertFalse(self._state_lock_owned(service))
            return original_load_summary(*args, **kwargs)

        def wrapped_db_execute(*args, **kwargs):
            sql = str(args[0] if args else kwargs.get("sql") or "")
            if "UPDATE runs" in sql:
                observed["update_runs"] += 1
                self.assertFalse(self._state_lock_owned(service))
            return original_db_execute(*args, **kwargs)

        with patch.object(service, "_load_run_summary", side_effect=wrapped_load_summary), patch.object(
            service, "_db_execute", side_effect=wrapped_db_execute
        ):
            result = service.report_result(
                task_id=task_id,
                hostname="judgehost-lock-report",
                payload={
                    "run_status": "ok",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/ac.cpp",
                        "tests": [],
                        "limits": {},
                        "usage": {},
                    },
                },
            )
        self.assertEqual(str(result.get("status") or ""), "ok")
        self.assertGreaterEqual(observed["load"], 1)
        self.assertEqual(observed["update_runs"], 1)

    def test_judgehost_domserver_backend_uses_queue_service(self) -> None:
        service = config.judgehost_task_service
        backend = JudgehostDomserverInvocationBackend(config.run_service, service)
        with patch.object(service, "enabled", return_value=True), patch.object(
            service, "auth_token_configured", return_value=True
        ), patch.object(service, "enqueue_task", return_value="jt-x") as mocked_enqueue, patch.object(
            service, "wait_for_task", return_value="r-x"
        ) as mocked_wait:
            run_id = backend.run_submission(
                problem="alice/sample",
                username="alice",
                build_id="b-x",
                submission_path="solutions/ac.cpp",
                run_id="r-x",
            )
        self.assertEqual(run_id, "r-x")
        self.assertEqual(mocked_enqueue.call_count, 1)
        self.assertEqual(mocked_wait.call_count, 1)

    def test_domjudge_endpoints_finalize_run(self) -> None:
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

        build_id = f"b-jh-dom-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        testcase_files = service.domjudge_get_testcase_files(testcase_id)
        self.assertEqual(len(testcase_files), 2)
        self.assertEqual({str(item.get("filename") or "") for item in testcase_files}, {"input", "output"})

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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("verdict") or ""), "OK")
        feedback_files = tests[0].get("feedback_files") if isinstance(tests[0], dict) else []
        self.assertIsInstance(feedback_files, list)
        self.assertTrue(any(str(item or "").startswith("feedback_dir/001/") for item in feedback_files))
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

    def test_domjudge_selected_tests_not_truncated_by_max_tests_per_task(self) -> None:
        service = config.judgehost_task_service
        old_enabled = service._enabled
        old_token = service._api_token
        old_username = service._api_username
        old_include_build_payload = service._include_build_payload
        old_max_tests_per_task = service._max_tests_per_task
        self.addCleanup(setattr, service, "_enabled", old_enabled)
        self.addCleanup(setattr, service, "_api_token", old_token)
        self.addCleanup(setattr, service, "_api_username", old_username)
        self.addCleanup(setattr, service, "_include_build_payload", old_include_build_payload)
        self.addCleanup(setattr, service, "_max_tests_per_task", old_max_tests_per_task)
        service._enabled = True
        service._api_token = "test-token"
        service._api_username = "judgehost"
        service._include_build_payload = True
        service._max_tests_per_task = 1

        build_id = f"b-jh-dom-notrunc-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-notrunc-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests" / "002.in").write_text("second\n", encoding="utf-8")
        (artifact_root / "ans" / "002.ans").write_text("second\n", encoding="utf-8")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in", "002.in"],
            invocation_id="inv-domjudge-notrunc",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="build.solve",
        )

        service.domjudge_register_host("judgehost-notrunc")
        rows = service.domjudge_fetch_work("judgehost-notrunc", max_batchsize=8)
        self.assertEqual(len(rows), 2)
        inputs_seen: set[str] = set()
        for row in rows:
            testcase_id = int(row.get("testcase_id") or 0)
            self.assertGreater(testcase_id, 0)
            files = service.domjudge_get_testcase_files(testcase_id)
            self.assertEqual({str(item.get("filename") or "") for item in files}, {"input", "output"})
            input_blob = next((str(item.get("content") or "") for item in files if str(item.get("filename") or "") == "input"), "")
            input_text = base64.b64decode(input_blob).decode("utf-8", errors="replace")
            inputs_seen.add(input_text)
        self.assertEqual(inputs_seen, {"ok\n", "second\n"})

    def test_domjudge_reuses_script_ids_for_same_hash_payload(self) -> None:
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

        build_id = f"b-jh-dom-cache-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-dom-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-dom-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-cache",
            invocation_run_ids=[run_id_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-cache",
            invocation_run_ids=[run_id_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
            force_recompile=True,
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

        self.assertEqual(compile_id_b, compile_id_a)
        self.assertEqual(run_id_num_b, run_id_num_a)
        self.assertEqual(compare_id_b, compare_id_a)

    def test_domjudge_multi_pass_summary_keeps_single_final_pass_and_strips_protocol_output(self) -> None:
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

        build_id = f"b-jh-dom-mp-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-mp-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps({"checker_mode": "testlib", "checker_args": [], "max_passes": 2}, indent=2) + "\n",
            encoding="utf-8",
        )

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="multi-pass",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-mp",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
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
        self.assertIn("feedback_dir/001/judgemessage.txt", [str(item or "") for item in (feedback_files or [])])

        run_root = config.fs_manager.resolve_run_root(run_id)
        self.assertFalse((run_root / "001.out").exists())

    def test_domjudge_rewrites_untrusted_non_tl_result_when_cpu_exceeds_time_limit(self) -> None:
        service = config.judgehost_task_service
        self.assertEqual(
            service._domjudge_rewrite_untrusted_runresult(
                "wrong-answer",
                cpu_sec=6.5,
                run_cfg_obj={"time_limit": 6.0},
            ),
            "timelimit",
        )
        self.assertEqual(
            service._domjudge_rewrite_untrusted_runresult(
                "run-error",
                cpu_sec=6.1,
                run_cfg_obj={"time_limit_ms": 6000},
            ),
            "timelimit",
        )
        self.assertEqual(
            service._domjudge_rewrite_untrusted_runresult(
                "wrong-answer",
                cpu_sec=5.9,
                run_cfg_obj={"time_limit": 6.0},
            ),
            "wrong-answer",
        )
        self.assertEqual(
            service._domjudge_rewrite_untrusted_runresult(
                "correct",
                cpu_sec=15.0,
                run_cfg_obj={"time_limit": 6.0},
            ),
            "correct",
        )
        self.assertEqual(
            service._domjudge_rewrite_untrusted_runresult(
                "run-error",
                cpu_sec=0.6,
                run_cfg_obj={"time_limit": 0.5},
            ),
            "timelimit",
        )
        self.assertEqual(
            service._domjudge_rewrite_untrusted_runresult(
                "run-error",
                cpu_sec=0.4,
                run_cfg_obj={"time_limit": 0.5},
            ),
            "run-error",
        )

    def test_domjudge_add_judging_run_rewrites_wa_to_tl_on_double_cpu(self) -> None:
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

        build_id = f"b-jh-dom-wa2tl-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-wa2tl-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps({"checker_mode": "testlib", "checker_args": [], "time_limit_ms": 6000}, indent=2) + "\n",
            encoding="utf-8",
        )

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-wa2tl",
            invocation_run_ids=[run_id],
            expected_behavior="unknown",
            invocation_source="run.execute",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        test_row = tests[0] if isinstance(tests[0], dict) else {}
        self.assertEqual(str(test_row.get("verdict") or ""), "TL")
        passes = test_row.get("passes") if isinstance(test_row, dict) else []
        self.assertIsInstance(passes, list)
        self.assertEqual(str((passes[0] or {}).get("verdict") or ""), "TL")

    def test_domjudge_reconnect_remaps_submitid_and_fetch_uses_new_value(self) -> None:
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

        build_id = f"b-jh-dom-reconnect-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-dom-reconnect-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-reconnect",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        self.assertTrue(task_id.startswith("jt-"))

        service.domjudge_register_host("judgehost-reconnect")
        first_rows = service.domjudge_fetch_work("judgehost-reconnect", max_batchsize=8)
        self.assertEqual(len(first_rows), 1)
        first = first_rows[0]
        job_id = int(first.get("jobid") or 0)
        self.assertGreater(job_id, 0)
        self.assertEqual(str(first.get("submitid") or ""), str(job_id))

        unfinished = service.domjudge_register_host("judgehost-reconnect")
        self.assertEqual(len(unfinished), 1)
        remapped_submitid = str(unfinished[0].get("submitid") or "")
        self.assertTrue(remapped_submitid.isdigit())
        self.assertNotEqual(remapped_submitid, str(job_id))

        second_rows = service.domjudge_fetch_work("judgehost-reconnect", max_batchsize=8)
        self.assertEqual(len(second_rows), 1)
        second = second_rows[0]
        self.assertEqual(int(second.get("jobid") or 0), job_id)
        self.assertEqual(str(second.get("submitid") or ""), remapped_submitid)

    def test_domjudge_fetch_work_skips_invalid_payload_task(self) -> None:
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

        build_bad = f"b-jh-dom-bad-{uuid.uuid4().hex[:8]}"
        run_bad = f"r-jh-dom-bad-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_bad)
        service._include_build_payload = False
        bad_task = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_bad,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_bad,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-bad",
            invocation_run_ids=[run_bad],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        build_good = f"b-jh-dom-good-{uuid.uuid4().hex[:8]}"
        run_good = f"r-jh-dom-good-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_good)
        service._include_build_payload = True
        good_task = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_good,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_good,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-good",
            invocation_run_ids=[run_good],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        service.domjudge_register_host("judgehost-skip-invalid")
        tasks = service.domjudge_fetch_work("judgehost-skip-invalid", max_batchsize=8)
        self.assertTrue(tasks)
        self.assertEqual(str(tasks[0].get("uuid") or ""), good_task)

        bad_task_row = service._task_by_id(bad_task)
        self.assertIsNotNone(bad_task_row)
        self.assertEqual(str(bad_task_row.get("status") or ""), service.STATUS_FAILED)
        self.assertIn("no tests in judgehost payload", str(bad_task_row.get("error_text") or ""))

        bad_run_row = config.db.fetch_one("SELECT status FROM runs WHERE id=?", [run_bad])
        self.assertIsNotNone(bad_run_row)
        self.assertEqual(str(bad_run_row["status"] or ""), "failed")

    def test_domjudge_reuses_cached_test_hash_but_exposes_case_id_as_testcase_id(self) -> None:
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

        build_a = f"b-jh-cache-a-{uuid.uuid4().hex[:8]}"
        run_a = f"r-jh-cache-a-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_a)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_a,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_a,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-cache-a",
            invocation_run_ids=[run_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        service.domjudge_register_host("judgehost-cache-a")
        rows_a = service.domjudge_fetch_work("judgehost-cache-a", max_batchsize=8)
        self.assertEqual(len(rows_a), 1)
        testcase_id_a = int(rows_a[0].get("testcase_id") or 0)
        case_id_a = int(rows_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        self.assertEqual(testcase_id_a, case_id_a)
        row_a = service._db_fetch_one(
            "SELECT testcase_id FROM judgehost_domjudge_cases WHERE id=?",
            [case_id_a],
        )
        self.assertIsNotNone(row_a)
        cached_testcase_id_a = int(row_a["testcase_id"] or 0)
        self.assertGreater(cached_testcase_id_a, 0)

        build_b = f"b-jh-cache-b-{uuid.uuid4().hex[:8]}"
        run_b = f"r-jh-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_b)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_b,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_b,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-cache-b",
            invocation_run_ids=[run_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        service.domjudge_register_host("judgehost-cache-b")
        rows_b = service.domjudge_fetch_work("judgehost-cache-b", max_batchsize=8)
        self.assertEqual(len(rows_b), 1)
        testcase_id_b = int(rows_b[0].get("testcase_id") or 0)
        case_id_b = int(rows_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertEqual(testcase_id_b, case_id_b)
        self.assertNotEqual(case_id_a, case_id_b)
        row_b = service._db_fetch_one(
            "SELECT testcase_id FROM judgehost_domjudge_cases WHERE id=?",
            [case_id_b],
        )
        self.assertIsNotNone(row_b)
        cached_testcase_id_b = int(row_b["testcase_id"] or 0)
        self.assertGreater(cached_testcase_id_b, 0)
        self.assertEqual(cached_testcase_id_a, cached_testcase_id_b)

    def test_domjudge_compare_script_shifts_framework_args_before_checker(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compare_script().decode("utf-8")
        self.assertIn("shift 3", script_text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "echo \"argc:$#\"\n"
                "if [ \"$#\" -eq 4 ]; then\n"
                "  exit 42\n"
                "fi\n"
                "exit 3\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ok\n", encoding="utf-8")
            test_ans.write_text("ok\n", encoding="utf-8")
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback), "--flag"],
                input="ok\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 42)
            checker_log = (feedback / "checker.log").read_text(encoding="utf-8", errors="replace")
            self.assertIn("argc:4", checker_log)

    def test_domjudge_compare_script_uses_testlib_arg_convention_with_stdin_team_output(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compare_script().decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "if [ \"$#\" -lt 3 ]; then\n"
                "  echo \"bad argc:$#\"\n"
                "  exit 3\n"
                "fi\n"
                "case \"$2\" in\n"
                "  */001.ans) ;;\n"
                "  *)\n"
                "    echo \"bad answer arg\"\n"
                "    exit 3\n"
                "    ;;\n"
                "esac\n"
                "case \"$3\" in\n"
                "  */feedback) ;;\n"
                "  *)\n"
                "    echo \"bad report output arg\"\n"
                "    exit 3\n"
                "    ;;\n"
                "esac\n"
                "read token || { echo \"Unexpected end of file - double expected\"; exit 43; }\n"
                "if [ \"$token\" = \"20\" ]; then\n"
                "  echo \"ok\"\n"
                "  exit 42\n"
                "fi\n"
                "echo \"wrong answer\"\n"
                "exit 43\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("ignored\n", encoding="utf-8")
            feedback.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 42)
            checker_log = (feedback / "checker.log").read_text(encoding="utf-8", errors="replace")
            self.assertIn("ok", checker_log)

    def test_domjudge_compare_script_preserves_checker_fail_exit_code(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compare_script().decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "echo \"FAIL Can not write to the result file (test case 1)\"\n"
                "exit 3\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("ignored\n", encoding="utf-8")
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("FAIL Can not write to the result file", result.stderr)
            judge_message = (feedback / "judgemessage.txt").read_text(encoding="utf-8", errors="replace")
            self.assertIn("FAIL Can not write to the result file", judge_message)

    def test_domjudge_compare_script_preserves_existing_judgemessage_on_checker_fail(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compare_script().decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "mkdir -p \"$3\"\n"
                "echo \"FAIL checker detailed message\" >\"$3/judgemessage.txt\"\n"
                "exit 3\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("ignored\n", encoding="utf-8")
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("FAIL checker detailed message", result.stderr)
            judge_message = (feedback / "judgemessage.txt").read_text(encoding="utf-8", errors="replace")
            self.assertIn("FAIL checker detailed message", judge_message)

    def test_domjudge_compare_script_in_build_solve_mode_accepts_without_answer(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compare_script(solve_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            feedback.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 42)

    def test_domjudge_interactive_pass_limit_is_forced_to_one(self) -> None:
        service = config.judgehost_task_service
        old_include_build_payload = service._include_build_payload
        self.addCleanup(setattr, service, "_include_build_payload", old_include_build_payload)
        service._include_build_payload = True

        build_id = f"b-jh-passlimit-interactive-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-passlimit-interactive-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps({"checker_mode": "testlib", "checker_args": [], "max_passes": 7}, indent=2) + "\n",
            encoding="utf-8",
        )
        interactor_bin = artifact_root / "bin" / "interactor"
        interactor_bin.parent.mkdir(parents=True, exist_ok=True)
        interactor_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(interactor_bin, 0o755)

        payload = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-passlimit-interactive",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        precomputed = payload.get("domjudge_precomputed") if isinstance(payload, dict) else {}
        run_cfg = precomputed.get("run_config") if isinstance(precomputed, dict) else {}
        self.assertIsInstance(run_cfg, dict)
        self.assertEqual(int(run_cfg.get("pass_limit") or 0), 1)

    def test_domjudge_multi_pass_uses_configured_pass_limit(self) -> None:
        service = config.judgehost_task_service
        old_include_build_payload = service._include_build_payload
        self.addCleanup(setattr, service, "_include_build_payload", old_include_build_payload)
        service._include_build_payload = True

        build_id = f"b-jh-passlimit-multipass-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-passlimit-multipass-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps({"checker_mode": "testlib", "checker_args": [], "max_passes": 7}, indent=2) + "\n",
            encoding="utf-8",
        )
        interactor_bin = artifact_root / "bin" / "interactor"
        interactor_bin.parent.mkdir(parents=True, exist_ok=True)
        interactor_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(interactor_bin, 0o755)

        payload = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="multi-pass",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-passlimit-multipass",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        precomputed = payload.get("domjudge_precomputed") if isinstance(payload, dict) else {}
        run_cfg = precomputed.get("run_config") if isinstance(precomputed, dict) else {}
        self.assertIsInstance(run_cfg, dict)
        self.assertEqual(int(run_cfg.get("pass_limit") or 0), 7)

    def test_domjudge_interactor_source_overrides_host_binary_payload(self) -> None:
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

        ws = Path(self._workspace_path())
        (ws / "interactors").mkdir(parents=True, exist_ok=True)
        (ws / "interactors" / "interactor.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )

        build_id = f"b-jh-interactor-source-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-interactor-source-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        interactor_bin = artifact_root / "bin" / "interactor"
        interactor_bin.parent.mkdir(parents=True, exist_ok=True)
        interactor_bin.write_bytes(b"\x7fELFfake-host-interactor")
        os.chmod(interactor_bin, 0o755)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="multi-pass",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-jh-interactor-source",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
            compile_only=False,
        )

        host = "judgehost-interactor-source"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next((row for row in tasks if str(row.get("uuid") or "") == task_id), None)
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

    def test_domjudge_compile_script_uses_configurable_flags(self) -> None:
        service = config.judgehost_task_service
        old_values = config.constants.to_dict()
        self.addCleanup(config.constants.replace, old_values)
        patched = dict(old_values)
        patched["TOOLCHAIN_CPP_COMPILER"] = "clang++"
        patched["TOOLCHAIN_JAVA_COMPILER"] = "javac-custom"
        patched["TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS"] = "-O3 -std=gnu++20 -DNDEBUG"
        patched["TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS"] = "--release 17 -encoding UTF-8"
        patched["TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS"] = "-X dev"
        config.constants.replace(patched)

        cpp_script = service._domjudge_compile_script("submission.cpp").decode("utf-8")
        java_script = service._domjudge_compile_script("submission.java").decode("utf-8")
        py_script = service._domjudge_compile_script("submission.py").decode("utf-8")
        self.assertIn('exec clang++ -O3 -std=gnu++20 -DNDEBUG -I. "$MAIN" -o "$DEST"', cpp_script)
        self.assertIn('javac-custom --release 17 -encoding UTF-8 "$SRC"', java_script)
        self.assertIn('"$PY" -X dev -m py_compile "$MAIN"', py_script)

    def test_domjudge_config_and_task_output_limits_use_kb_units(self) -> None:
        service = config.judgehost_task_service
        cfg = service.domjudge_config()
        self.assertEqual(str(cfg.get("timelimit_overshoot") or ""), "1s|100%")
        self.assertEqual(
            int(cfg.get("output_storage_limit") or 0),
            int(getattr(service._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536) * 1024,
        )
        self.assertEqual(
            int(cfg.get("script_filesize_limit") or 0),
            int(getattr(service._constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536),
        )

    def test_domjudge_python_compile_script_works_without_entry_point_env(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compile_script("submission.py").decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "run"
            source = root / "submission.py"
            dest = root / "program"
            source.write_text("print('ok')\n", encoding="utf-8")
            script.write_text(script_text, encoding="utf-8")
            os.chmod(script, 0o755)
            env = dict(os.environ)
            env.pop("ENTRY_POINT", None)
            result = subprocess.run(
                [str(script), str(dest), "262144", str(source)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(dest.exists())
            launcher = dest.read_text(encoding="utf-8", errors="replace")
            self.assertIn("exec ", launcher)
            self.assertIn("submission.py", launcher)

    def test_domjudge_interactive_run_script_uses_official_runpipe_wrapper(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(True, solve_mode=False).decode("utf-8")
        self.assertIn("runpipe", script_text)
        self.assertIn("runjury", script_text)
        self.assertIn("TESTOUT", script_text)
        self.assertIn("META", script_text)
        self.assertNotIn("INTERACTOR_BIN", script_text)

    def test_domjudge_cpp_executable_build_script_comes_from_asset(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_cpp_executable_build_script(
            "interactor.cpp",
            role="interactor",
        ).decode("utf-8")
        self.assertIn("#!/bin/sh", script_text)
        self.assertIn("Auto-generated build script for interactor by Polygon2DOMjudge", script_text)
        self.assertIn("g++ -Wall -DDOMJUDGE -O2 interactor.cpp -std=gnu++20 -o run", script_text)

    def test_domjudge_generate_run_script_executes_submission_runner_with_payload_args(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(
            False,
            solve_mode=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            submission_runner = root / "program"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\" 7\n", encoding="utf-8")
            submission_runner.write_text("#!/bin/sh\nprintf 'runner:%s\\n' \"$1\"\n", encoding="utf-8")
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "runner:7\n")

    def test_domjudge_generate_run_script_handles_option_like_payload_args(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(
            False,
            solve_mode=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            submission_runner = root / "program"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\" -n\n", encoding="utf-8")
            submission_runner.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n", encoding="utf-8")
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "-n\n")

    def test_domjudge_generate_run_script_preserves_wrapper_command_vector_when_appending_payload(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(
            False,
            solve_mode=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            wrapper = root / "wrapper"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\" 7 8\n", encoding="utf-8")
            wrapper.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "for arg in \"$@\"; do\n"
                "  printf '%s\\n' \"$arg\"\n"
                "done\n",
                encoding="utf-8",
            )
            os.chmod(wrapper, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(wrapper), "A", "B", "C"],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                prog_out.read_text(encoding="utf-8").splitlines(),
                ["A", "B", "C", "7", "8"],
            )

    def test_domjudge_generate_run_script_supports_plain_argument_payload(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(
            False,
            solve_mode=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            submission_runner = root / "program"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("9\n", encoding="utf-8")
            submission_runner.write_text("#!/bin/sh\nprintf 'plain:%s\\n' \"$1\"\n", encoding="utf-8")
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "plain:9\n")

    def test_domjudge_generate_run_script_accepts_submission_bin_only_payload(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(
            False,
            solve_mode=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            case_dir = root / "case"
            test_in = case_dir / "001.in"
            prog_out = case_dir / "program.out"
            submission_runner = root / "program"
            unrelated_cwd = root / "other-cwd"
            case_dir.mkdir(parents=True, exist_ok=True)
            unrelated_cwd.mkdir(parents=True, exist_ok=True)
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\"\n", encoding="utf-8")
            submission_runner.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "if [ \"$#\" -eq 0 ]; then\n"
                "  printf 'no-extra-args\\n'\n"
                "  exit 0\n"
                "fi\n"
                "printf 'unexpected:%s\\n' \"$1\"\n",
                encoding="utf-8",
            )
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=unrelated_cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "no-extra-args\n")

    def test_domjudge_generate_compare_script_runs_validator(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compare_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            team_out = root / "program.out"
            validator = root / "validator"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            test_in.write_text("", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            team_out.write_text("42\n", encoding="utf-8")
            validator.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "read -r token || exit 1\n"
                "[ \"$token\" = \"42\" ] || exit 1\n",
                encoding="utf-8",
            )
            os.chmod(validator, 0o755)

            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)

            team_out.write_text("41\n", encoding="utf-8")
            bad = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(bad.returncode, 43, bad.stderr)

    def test_domjudge_generate_compare_script_compiles_validator_from_readonly_script_dir(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            work_dir = root / "work"
            feedback = work_dir / "feedback"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            compare_script = scripts_dir / "run"
            validator_src = scripts_dir / "validator.cpp"
            test_in = work_dir / "001.in"
            test_ans = work_dir / "001.ans"
            team_out = work_dir / "program.out"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            validator_src.write_text(
                "#include <cstdio>\n"
                "int main(){ long long x=0; if(std::scanf(\"%lld\", &x)!=1) return 1; return x==42 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            test_in.write_text("", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            team_out.write_text("42\n", encoding="utf-8")
            os.chmod(scripts_dir, 0o555)
            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=work_dir,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)

    def test_domjudge_generate_invocation_uses_generate_scripts_and_validator_payload(self) -> None:
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

        build_id = f"b-jh-generate-scripts-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-generate-scripts-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        validator_bin = artifact_root / "bin" / "validator"
        validator_bin.parent.mkdir(parents=True, exist_ok=True)
        validator_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(validator_bin, 0o755)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-jh-generate-scripts",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="build.generate-input",
            compile_only=False,
        )
        host = "judgehost-generate-scripts"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next((row for row in tasks if str(row.get("uuid") or "") == task_id), None)
        self.assertIsNotNone(task_row)

        run_files = service.domjudge_get_executable_files("run", str(task_row.get("run_script_id") or ""))
        run_item = next((item for item in run_files if str(item.get("filename") or "") == "run"), {})
        run_text = base64.b64decode(str(run_item.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("missing generate command payload", run_text)

        compare_files = service.domjudge_get_executable_files("compare", str(task_row.get("compare_script_id") or ""))
        compare_run = next((item for item in compare_files if str(item.get("filename") or "") == "run"), {})
        compare_text = base64.b64decode(str(compare_run.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("VALIDATOR_BIN", compare_text)
        self.assertTrue(any(str(item.get("filename") or "") == "validator" for item in compare_files))

    def test_domjudge_generate_invocation_interactive_mode_does_not_require_interactor_payload(self) -> None:
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

        build_id = f"b-jh-generate-interactive-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-generate-interactive-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        validator_bin = artifact_root / "bin" / "validator"
        validator_bin.parent.mkdir(parents=True, exist_ok=True)
        validator_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(validator_bin, 0o755)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-jh-generate-interactive",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="build.generate-input",
            compile_only=False,
        )
        host = "judgehost-generate-interactive"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next((row for row in tasks if str(row.get("uuid") or "") == task_id), None)
        self.assertIsNotNone(task_row)

        run_files = service.domjudge_get_executable_files("run", str(task_row.get("run_script_id") or ""))
        run_names = {str(item.get("filename") or "") for item in run_files}
        self.assertIn("run", run_names)
        self.assertNotIn("interactor.cpp", run_names)
        run_item = next((item for item in run_files if str(item.get("filename") or "") == "run"), {})
        run_text = base64.b64decode(str(run_item.get("content") or "")).decode("utf-8", errors="replace")
        self.assertIn("missing generate command payload", run_text)

        compare_files = service.domjudge_get_executable_files("compare", str(task_row.get("compare_script_id") or ""))
        self.assertTrue(any(str(item.get("filename") or "") == "validator" for item in compare_files))

    def test_domjudge_run_script_compile_only_branch_uses_skip_run_copy(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(False, solve_mode=False, compile_only=True).decode("utf-8")
        self.assertIn('cat "$TESTIN" >"$PROGOUT"', script_text)
        self.assertIn('"$@" </dev/null >/dev/null', script_text)

    def test_domjudge_run_script_manual_validate_branch_copies_input_to_output(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_run_script(
            False,
            solve_mode=False,
            compile_only=False,
            manual_validate_only=True,
        ).decode("utf-8")
        self.assertIn('cat "$TESTIN" >"$PROGOUT"', script_text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run-wrapper"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            noop = root / "noop.sh"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("manual input\n", encoding="utf-8")
            noop.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(noop, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(noop)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "manual input\n")

    def test_domjudge_skip_compile_creates_noop_executable(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compile_script(
            "manual_validate.cpp",
            manual_validate_only=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compile_script = root / "run"
            dest = root / "program"
            source = root / "manual_validate.cpp"
            compile_script.write_text(script_text, encoding="utf-8")
            os.chmod(compile_script, 0o755)
            source.write_text("int main(){return 0;}\n", encoding="utf-8")
            result = subprocess.run(
                [str(compile_script), str(dest), "65536", str(source)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(dest.exists())
            self.assertTrue(os.access(dest, os.X_OK))

    def test_domjudge_compile_script_matches_official_wrapper_shape(self) -> None:
        service = config.judgehost_task_service
        script_text = service._domjudge_compile_script("submission.cpp").decode("utf-8")
        self.assertIn('exec g++ -x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE -I. "$MAIN" -o "$DEST"', script_text)

    def test_domjudge_compile_only_cpp_script_compiles_then_writes_noop_program(self) -> None:
        service = config.judgehost_task_service
        compile_text = service._domjudge_compile_script("submission.cpp", compile_only=True).decode("utf-8")
        run_text = service._domjudge_run_script(False, solve_mode=False, compile_only=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compile_script = root / "compile-wrapper"
            run_script = root / "run-wrapper"
            dest = root / "program"
            source = root / "submission.cpp"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            compile_script.write_text(compile_text, encoding="utf-8")
            run_script.write_text(run_text, encoding="utf-8")
            os.chmod(compile_script, 0o755)
            os.chmod(run_script, 0o755)
            source.write_text(
                "#include <iostream>\n"
                "int main(){ int *p=nullptr; std::cout << *p << '\\n'; return 0; }\n",
                encoding="utf-8",
            )
            test_in.write_text("compile-only\n", encoding="utf-8")
            compiled = subprocess.run(
                [str(compile_script), str(dest), "65536", str(source)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertTrue(dest.exists())
            self.assertTrue(os.access(dest, os.X_OK))
            self.assertEqual(dest.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")
            executed = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(dest)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "compile-only\n")

    def test_domjudge_compile_only_uses_single_virtual_case_even_with_build_tests(self) -> None:
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

        build_id = f"b-jh-compile-only-virtual-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-virtual-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-virtual",
            invocation_run_ids=[run_id],
            expected_behavior="compile",
            invocation_source="build.compile",
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-virtual")
        tasks = service.domjudge_fetch_work("judgehost-compile-only-virtual", max_batchsize=16)
        task_rows = [row for row in tasks if str(row.get("uuid") or "") == task_id]
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
        db_rows = service._db_fetch_all(
            "SELECT id,test_name,status FROM judgehost_domjudge_cases WHERE run_id=? ORDER BY ordinal ASC, id ASC",
            [run_id],
        )
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

        build_id = f"b-jh-compile-only-multipass-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-multipass-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        interactor_bin = artifact_root / "bin" / "interactor"
        interactor_bin.parent.mkdir(parents=True, exist_ok=True)
        interactor_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(interactor_bin, 0o755)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="multi-pass",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-multipass",
            invocation_run_ids=[run_id],
            expected_behavior="compile",
            invocation_source="build.compile",
            compile_only=True,
        )
        host = "judgehost-compile-only-multipass"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next((row for row in tasks if str(row.get("uuid") or "") == task_id), None)
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

        host = "judgehost-compile-only-extra-cache"
        build_id = f"b-jh-compile-only-extra-cache-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        extra_testlib = base64.b64encode(b"// testlib\n").decode("ascii")
        prepared = {"extra_sources_b64": {"testlib.h": extra_testlib}}

        run_a = f"r-jh-compile-only-extra-a-{uuid.uuid4().hex[:8]}"
        task_a = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_a,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-extra-a",
            invocation_run_ids=[run_a],
            expected_behavior="compile",
            invocation_source="build.compile",
            compile_only=True,
            prepared_payload=prepared,
        )
        service.domjudge_register_host(host)
        rows_a = service.domjudge_fetch_work(host, max_batchsize=16)
        task_rows_a = [row for row in rows_a if str(row.get("uuid") or "") == task_a]
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

        run_b = f"r-jh-compile-only-extra-b-{uuid.uuid4().hex[:8]}"
        task_b = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_b,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-extra-b",
            invocation_run_ids=[run_b],
            expected_behavior="compile",
            invocation_source="build.compile",
            compile_only=True,
            prepared_payload=prepared,
        )
        rows_b = service.domjudge_fetch_work(host, max_batchsize=16)
        self.assertFalse(any((str(row.get("uuid") or "") == task_b for row in rows_b)))
        self.assertEqual(service.wait_for_task(task_b, timeout_sec=2.0), run_b)
        run_row_b = config.db.fetch_one("SELECT status FROM runs WHERE id=?", [run_b])
        self.assertIsNotNone(run_row_b)
        self.assertEqual(str(run_row_b["status"] or "").strip().lower(), "ok")

    def test_domjudge_compile_only_cache_hit_without_build_payload_tests(self) -> None:
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

        host = "judgehost-compile-only-empty-build-payload-cache"
        # save-source compile check path uses a placeholder build id and no build payload tests
        build_id = "pending"

        run_a = f"r-jh-compile-only-empty-build-a-{uuid.uuid4().hex[:8]}"
        task_a = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="tmp.cpp",
            run_id=run_a,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-empty-build-a",
            invocation_run_ids=[run_a],
            expected_behavior="compile",
            invocation_source="problem.solution.save_source",
            compile_only=True,
        )
        service.domjudge_register_host(host)
        rows_a = service.domjudge_fetch_work(host, max_batchsize=16)
        task_rows_a = [row for row in rows_a if str(row.get("uuid") or "") == task_a]
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
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="tmp.cpp",
            run_id=run_b,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-empty-build-b",
            invocation_run_ids=[run_b],
            expected_behavior="compile",
            invocation_source="problem.solution.save_source",
            compile_only=True,
        )
        rows_b = service.domjudge_fetch_work(host, max_batchsize=16)
        self.assertFalse(any((str(row.get("uuid") or "") == task_b for row in rows_b)))
        self.assertEqual(service.wait_for_task(task_b, timeout_sec=2.0), run_b)
        run_row_b = config.db.fetch_one("SELECT status FROM runs WHERE id=?", [run_b])
        self.assertIsNotNone(run_row_b)
        self.assertEqual(str(run_row_b["status"] or "").strip().lower(), "ok")

    def test_enqueue_task_merges_prepared_payload_with_base_payload(self) -> None:
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

        build_id = f"b-jh-prepared-merge-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-prepared-merge-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        extra_testlib = base64.b64encode(b"// testlib\n").decode("ascii")

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-prepared-merge",
            invocation_run_ids=[run_id],
            expected_behavior="compile",
            invocation_source="build.compile",
            compile_only=True,
            prepared_payload={"extra_sources_b64": {"testlib.h": extra_testlib}},
        )
        task = service._task_by_id(task_id)
        self.assertIsNotNone(task)
        payload = task.get("payload") if isinstance(task, dict) else {}
        self.assertIsInstance(payload, dict)
        self.assertEqual(str(payload.get("source_name") or ""), "gen.cpp")
        self.assertTrue(bool(str(payload.get("source_b64") or "").strip()))
        self.assertIsInstance(payload.get("build_payload"), dict)
        extras = payload.get("extra_sources_b64") if isinstance(payload, dict) else {}
        self.assertIsInstance(extras, dict)
        self.assertEqual(str(extras.get("testlib.h") or ""), extra_testlib)
        self.assertTrue(bool(payload.get("compile_only")))

    def test_domjudge_source_files_include_prepared_extra_sources(self) -> None:
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
        self._reset_task_queue_state(service)

        build_id = f"b-jh-extra-src-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-extra-src-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        extra_testlib = base64.b64encode(b"// testlib helper\n").decode("ascii")

        _task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b'#include "testlib.h"\nint main(){return 0;}\n',
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-extra-src",
            invocation_run_ids=[run_id],
            expected_behavior="compile",
            invocation_source="build.compile",
            compile_only=True,
            prepared_payload={"extra_sources_b64": {"testlib.h": extra_testlib}},
        )
        service.domjudge_register_host("judgehost-extra-src")
        work_rows = service.domjudge_fetch_work("judgehost-extra-src", max_batchsize=16)
        self.assertTrue(work_rows)
        work_row = next((row for row in work_rows if str(row.get("uuid") or "") == _task_id), None)
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

        build_id = f"b-jh-compile-only-ok-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-ok-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        shutil.rmtree(artifact_root / "tests", ignore_errors=True)
        shutil.rmtree(artifact_root / "ans", ignore_errors=True)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-ok",
            invocation_run_ids=[run_id],
            expected_behavior="compile",
            invocation_source="build.compile",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        self.assertTrue(bool(summary.get("compile_only")))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(str((tests[0] or {}).get("verdict") or ""), "OK")

    def test_domjudge_compile_only_missing_output_is_normalized_to_ok(self) -> None:
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

        build_id = f"b-jh-compile-only-no-output-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-no-output-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        shutil.rmtree(artifact_root / "tests", ignore_errors=True)
        shutil.rmtree(artifact_root / "ans", ignore_errors=True)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){return 0;}\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-no-output",
            invocation_run_ids=[run_id],
            expected_behavior="compile",
            invocation_source="build.compile",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(str((tests[0] or {}).get("verdict") or ""), "OK")
        passes = (tests[0] or {}).get("passes") if tests else []
        first_pass = (passes[0] if isinstance(passes, list) and passes else {})
        self.assertFalse(str((first_pass or {}).get("output_ref") or "").strip())

    def test_domjudge_compile_only_result_normalization_maps_compile_failure_to_ce(self) -> None:
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

        build_id = f"b-jh-compile-only-ce-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compile-only-ce-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        shutil.rmtree(artifact_root / "tests", ignore_errors=True)
        shutil.rmtree(artifact_root / "ans", ignore_errors=True)

        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b"int main(){ return syntax_error }\n",
            upload_filename="checker.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-compile-only-ce",
            invocation_run_ids=[run_id],
            expected_behavior="compile",
            invocation_source="build.compile",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
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

        build_id = f"b-jh-extra-source-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-extra-source-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=b'#include "testlib.h"\nint main(){return 0;}\n',
            upload_filename="gen.cpp",
            run_id=run_id,
            selected_tests=[],
            invocation_id="inv-jh-extra-source",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="build.generate-input",
            task_kind="generate",
            prepared_payload={
                "build_payload": {
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
                            "max_passes": 1,
                            "time_limit_ms": 30000,
                            "memory_limit_mb": 1024,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "problem_limits": {"time_limit_ms": 30000, "memory_limit_mb": 1024},
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
        source_files = service.domjudge_get_source_files(str(tasks[0].get("jobid") or ""))
        source_names = {str(item.get("filename") or "") for item in source_files}
        self.assertIn("gen.cpp", source_names)
        self.assertIn("testlib.h", source_names)

    def test_domjudge_b64_decode_requires_base64_text(self) -> None:
        service = config.judgehost_task_service
        blob = b"ok\n"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(service._domjudge_b64_decode(encoded), blob)
        self.assertEqual(service._domjudge_b64_decode(encoded.encode("ascii")), blob)
        with self.assertRaises(RuntimeError):
            service._domjudge_b64_decode(b"binary-artifact")
        with self.assertRaises(RuntimeError):
            service._domjudge_b64_decode("%not-base64%")

    def test_domjudge_payload_blob_bytes_keeps_raw_upload_contract(self) -> None:
        service = config.judgehost_task_service
        blob = b"binary-artifact"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(service._domjudge_payload_blob_bytes(blob), blob)
        self.assertEqual(service._domjudge_payload_blob_bytes(encoded), blob)

    def test_domjudge_strip_protocol_trace_removes_runpipe_transcript_lines(self) -> None:
        cleaned = config.judgehost_task_service._domjudge_strip_protocol_trace(
            b"[  0.019s/6]>: 1 100\n"
            b"hello\n"
            b"[  0.054s/4]<: ? 0\n"
            b"[  0.071s/0]]\n"
            b"\n"
        )
        self.assertEqual(cleaned.decode("utf-8"), "hello\n")

    def test_domjudge_feedback_line_parser_keeps_first_non_empty_line(self) -> None:
        service = config.judgehost_task_service
        self.assertEqual(service._domjudge_feedback_line_from_text("\n\nfailed on pass 2\nignored"), "failed on pass 2")
        self.assertEqual(service._domjudge_feedback_line_from_bytes(b"\r\nmessage\r\nsecond"), "message")

    def test_domjudge_add_judging_run_endpoint_accepts_large_multipart_payload(self) -> None:
        from app.main import app

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

        build_id = f"b-jh-large-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-large-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-large",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        service.domjudge_register_host("judgehost-large")
        tasks = service.domjudge_fetch_work("judgehost-large", max_batchsize=1)
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        large_output = b"A" * (20 * 1024 * 1024)
        metadata = b"cpu-time: 0.001\nwall-time: 0.001\nmemory-bytes: 4096\n"

        with TestClient(app) as client:
            headers = {"Authorization": "Bearer test-token"}
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

        row = service._db_fetch_one(
            "SELECT status,runresult,output_run_rel,output_diff_rel,metadata_rel FROM judgehost_domjudge_cases WHERE id=?",
            [case_id],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "reported")
        self.assertEqual(str(row["runresult"] or ""), "correct")
        self.assertTrue(str(row["output_run_rel"] or "").strip())
        self.assertTrue(str(row["output_diff_rel"] or "").strip())
        self.assertTrue(str(row["metadata_rel"] or "").strip())

    def test_domjudge_large_multipart_keeps_starlette_file_spool_threshold(self) -> None:
        from app.main import app

        service = config.judgehost_task_service
        old_enabled = service._enabled
        old_token = service._api_token
        old_username = service._api_username
        old_include_build_payload = service._include_build_payload
        old_max_part_size = int(getattr(MultiPartParser, "max_part_size", 0) or 0)
        old_max_file_size = int(getattr(MultiPartParser, "max_file_size", 0) or 0)
        self.addCleanup(setattr, service, "_enabled", old_enabled)
        self.addCleanup(setattr, service, "_api_token", old_token)
        self.addCleanup(setattr, service, "_api_username", old_username)
        self.addCleanup(setattr, service, "_include_build_payload", old_include_build_payload)
        self.addCleanup(setattr, MultiPartParser, "max_part_size", old_max_part_size)
        self.addCleanup(setattr, MultiPartParser, "max_file_size", old_max_file_size)
        service._enabled = True
        service._api_token = "test-token"
        service._api_username = "judgehost"
        service._include_build_payload = True
        MultiPartParser.max_part_size = 1024 * 1024
        MultiPartParser.max_file_size = 1024 * 1024

        build_id = f"b-jh-spool-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-spool-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-domjudge-spool",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        build_id = f"b-jh-limits-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-limits-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        run_cfg_path = self._build_artifact_root(build_id) / "logs" / "run_config.json"
        if run_cfg_path.exists():
            run_cfg_path.unlink()
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="interactive",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="",
            invocation_run_ids=[],
            expected_behavior="accepted",
            invocation_source="build.solve",
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
        self.assertEqual(
            int(run_config.get("output_limit") or 0),
            int(getattr(service._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536),
        )
        self.assertEqual(int(run_config.get("pass_limit") or 0), 1)
        self.assertEqual(
            int(compare_config.get("script_filesize_limit") or 0),
            int(getattr(service._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536),
        )
        self.assertEqual(
            int(compile_config.get("script_filesize_limit") or 0),
            int(getattr(service._constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536),
        )

    def test_domjudge_build_solve_multi_pass_defaults_pass_limit_when_run_config_missing(self) -> None:
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

        ws = Path(self._workspace_path())
        (ws / "config").mkdir(parents=True, exist_ok=True)
        (ws / "interactors").mkdir(parents=True, exist_ok=True)
        (ws / "config" / "problem.json").write_text(
            json.dumps({"time_limit_ms": 2000, "memory_limit_mb": 1024, "mode": "multi-pass"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (ws / "interactors" / "interactor.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )

        build_id = f"b-jh-multipass-default-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-multipass-default-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        run_cfg_path = self._build_artifact_root(build_id) / "logs" / "run_config.json"
        if run_cfg_path.exists():
            run_cfg_path.unlink()

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="multi-pass",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="",
            invocation_run_ids=[],
            expected_behavior="accepted",
            invocation_source="build.solve",
        )

        service.domjudge_register_host("judgehost-multipass-default")
        tasks = service.domjudge_fetch_work("judgehost-multipass-default", max_batchsize=1)
        self.assertEqual(len(tasks), 1)
        run_config_raw = str(tasks[0].get("run_config") or "{}")
        run_config = json.loads(run_config_raw)
        self.assertEqual(int(run_config.get("pass_limit") or 0), 16)

    def test_domjudge_build_solve_output_cache_hits_expected_accepted_runs(self) -> None:
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

        build_id = f"b-jh-solve-cache-{uuid.uuid4().hex[:8]}"
        run_id_solve = f"r-jh-solve-cache-{uuid.uuid4().hex[:8]}"
        run_id_exec = f"r-jh-exec-cache-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests" / "001.in").write_text("42\n", encoding="utf-8")
        (artifact_root / "ans" / "001.ans").write_text("42\n", encoding="utf-8")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_solve,
            selected_tests=["001.in"],
            invocation_id="inv-buildsolve-cache",
            invocation_run_ids=[run_id_solve],
            expected_behavior="accepted",
            invocation_source="build.solve",
        )

        service.domjudge_register_host("judgehost-solve-cache")
        tasks = service.domjudge_fetch_work("judgehost-solve-cache", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-solve-cache",
            case_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        meta_text = "cpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-solve-cache",
            case_id,
            {
                "runresult": "correct",
                "runtime": "0.004",
                "output_run": base64.b64encode(b"42\n").decode("ascii"),
                "output_diff": base64.b64encode(b"ok\n").decode("ascii"),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )
        solved_row = config.db.fetch_one("SELECT status FROM runs WHERE id=?", [run_id_solve])
        self.assertIsNotNone(solved_row)
        self.assertEqual(str(solved_row["status"] or ""), "ok")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_exec,
            selected_tests=["001.in"],
            invocation_id="inv-run-cache-hit",
            invocation_run_ids=[run_id_exec],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        cached_fetch = service.domjudge_fetch_work("judgehost-solve-cache", max_batchsize=8)
        self.assertEqual(cached_fetch, [])

        run_row = None
        for _ in range(4):
            run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_exec])
            if run_row is not None and str(run_row["status"] or "").strip().lower() in {"ok", "failed"}:
                break
            service.domjudge_fetch_work("judgehost-solve-cache", max_batchsize=8)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or ""), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("verdict") or ""), "OK")
        passes = tests[0].get("passes") if isinstance(tests[0], dict) else []
        self.assertIsInstance(passes, list)
        self.assertTrue(str((passes[0] or {}).get("output_ref") or "").strip())
        run_root = config.fs_manager.resolve_run_root(run_id_exec)
        self.assertFalse((run_root / "001.out").exists())

        case_row = service._db_fetch_one(
            "SELECT status FROM judgehost_domjudge_cases WHERE run_id=? ORDER BY id ASC LIMIT 1",
            [run_id_exec],
        )
        self.assertIsNotNone(case_row)
        self.assertEqual(str(case_row["status"] or ""), "reported")

    def test_domjudge_prequeue_cache_consumes_per_case_and_leases_only_misses(self) -> None:
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

        build_id = f"b-jh-partial-cache-{uuid.uuid4().hex[:8]}"
        run_id_seed = f"r-jh-partial-seed-{uuid.uuid4().hex[:8]}"
        run_id_target = f"r-jh-partial-target-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests" / "002.in").write_text("miss\n", encoding="utf-8")
        (artifact_root / "ans" / "002.ans").write_text("miss\n", encoding="utf-8")
        service.domjudge_register_host("judgehost-partial-cache")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_seed,
            selected_tests=["001.in"],
            invocation_id="inv-jh-partial-seed",
            invocation_run_ids=[run_id_seed],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_target,
            selected_tests=["001.in", "002.in"],
            invocation_id="inv-jh-partial-target",
            invocation_run_ids=[run_id_target],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        rows = service._db_fetch_all(
            "SELECT id,test_name,status FROM judgehost_domjudge_cases WHERE run_id=? ORDER BY ordinal ASC, id ASC",
            [run_id_target],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0]["test_name"] or ""), "001.in")
        self.assertEqual(str(rows[0]["status"] or ""), "reported")
        self.assertEqual(str(rows[1]["test_name"] or ""), "002.in")
        self.assertEqual(str(rows[1]["status"] or ""), "pending")
        expected_case_id = int(rows[1]["id"])

        leased = service.domjudge_fetch_work("judgehost-partial-cache", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        self.assertEqual(int(leased[0].get("judgetaskid") or 0), expected_case_id)

    def test_domjudge_shared_pending_job_ignores_prequeue_owned_job(self) -> None:
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

        build_id = f"b-jh-prequeue-owned-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-prequeue-owned-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        payload = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-jh-prequeue-owned",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        task_id = f"t-jh-prequeue-owned-{uuid.uuid4().hex[:8]}"
        job_id = int(
            service._domjudge_prepare_job(
                "prequeue-cache",
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "payload": payload,
                },
            )
        )
        job_row = service._db_fetch_one(
            "SELECT lease_owner,status FROM judgehost_domjudge_jobs WHERE job_id=?",
            [job_id],
        )
        self.assertIsNotNone(job_row)
        self.assertEqual(str(job_row["lease_owner"] or ""), "prequeue-cache")
        self.assertEqual(str(job_row["status"] or ""), "leased")

        service.domjudge_register_host("judgehost-prequeue-owned")
        leased = service.domjudge_fetch_work("judgehost-prequeue-owned", max_batchsize=8)
        self.assertEqual(leased, [])

    def test_domjudge_release_prepared_job_preserves_real_host_lease(self) -> None:
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

        build_id = f"b-jh-prequeue-release-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-prequeue-release-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        payload = service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-jh-prequeue-release",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )

        task_id = f"t-jh-prequeue-release-{uuid.uuid4().hex[:8]}"
        job_id = int(
            service._domjudge_prepare_job(
                "prequeue-cache",
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "payload": payload,
                },
            )
        )
        leased = service._domjudge_lease_cases(job_id, "judgehost-prequeue-release", 8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service._domjudge_release_prepared_job_for_queue(job_id)

        case_row = service._db_fetch_one(
            "SELECT status,lease_owner FROM judgehost_domjudge_cases WHERE id=?",
            [case_id],
        )
        self.assertIsNotNone(case_row)
        self.assertEqual(str(case_row["status"] or ""), "leased")
        self.assertEqual(str(case_row["lease_owner"] or ""), "judgehost-prequeue-release")

        job_row = service._db_fetch_one(
            "SELECT status,lease_owner FROM judgehost_domjudge_jobs WHERE job_id=?",
            [job_id],
        )
        self.assertIsNotNone(job_row)
        self.assertEqual(str(job_row["status"] or ""), "leased")
        self.assertEqual(str(job_row["lease_owner"] or ""), "judgehost-prequeue-release")

    def test_domjudge_cache_shortcut_requires_output_blob_for_ok_result(self) -> None:
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

        build_id = f"b-jh-cache-blob-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-cache-blob-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-cache-blob-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-cache-blob")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            invocation_id="inv-cache-blob-a",
            invocation_run_ids=[run_id_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        cache_root = Path(config.settings.cache_root) / "judge-fs-index" / "v2" / service.CASE_CACHE_KIND
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
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            invocation_id="inv-cache-blob-b",
            invocation_run_ids=[run_id_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-cache-blob", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertGreater(int(tasks_b[0].get("judgetaskid") or 0), 0)

    def test_domjudge_cache_manifest_mismatch_is_deleted_and_treated_as_miss(self) -> None:
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

        build_id = f"b-jh-manifest-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-manifest-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-manifest-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-manifest")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            invocation_id="inv-manifest-a",
            invocation_run_ids=[run_id_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        cache_root = Path(config.settings.cache_root) / "judge-fs-index" / "v2" / service.CASE_CACHE_KIND
        files_dirs = [p for p in cache_root.rglob("files") if p.is_dir() and (not p.is_symlink())]
        self.assertTrue(files_dirs)
        target_entry_dir = sorted(files_dirs)[-1].parent
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
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            invocation_id="inv-manifest-b",
            invocation_run_ids=[run_id_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-manifest", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertFalse(target_entry_dir.exists())

    def test_domjudge_cache_blob_sha_mismatch_is_deleted_and_treated_as_miss(self) -> None:
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

        build_id = f"b-jh-blobsha-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-blobsha-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-blobsha-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-blobsha")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            invocation_id="inv-blobsha-a",
            invocation_run_ids=[run_id_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        cache_root = Path(config.settings.cache_root) / "judge-fs-index" / "v2" / service.CASE_CACHE_KIND
        blob_paths = [p for p in cache_root.rglob("program.out") if p.is_file() and (not p.is_symlink())]
        self.assertTrue(blob_paths)
        target_blob = sorted(blob_paths)[-1]
        target_entry_dir = target_blob.parent.parent.resolve()
        target_blob.write_bytes(b"tampered\n")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            invocation_id="inv-blobsha-b",
            invocation_run_ids=[run_id_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-blobsha", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertFalse(target_entry_dir.exists())

    def test_domjudge_expected_accepted_does_not_shortcut_non_ok_cache(self) -> None:
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

        build_id = f"b-jh-accepted-cache-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-accepted-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-accepted-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-accepted-cache")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            invocation_id="inv-accepted-cache-a",
            invocation_run_ids=[run_id_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        failed_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_a])
        self.assertIsNotNone(failed_row)
        self.assertEqual(str(failed_row["status"] or "").strip().lower(), "ok")
        failed_summary = json.loads(str(failed_row["summary_json"] or "{}"))
        failed_tests = failed_summary.get("tests") if isinstance(failed_summary, dict) else []
        self.assertIsInstance(failed_tests, list)
        self.assertEqual(str((failed_tests[0] or {}).get("verdict") or ""), "WA")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            invocation_id="inv-accepted-cache-b",
            invocation_run_ids=[run_id_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-accepted-cache", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        self.assertGreater(int(tasks_b[0].get("judgetaskid") or 0), 0)

    def test_domjudge_compare_exitcode_3_is_tagged_checker_fail(self) -> None:
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

        build_id = f"b-jh-checker-fail-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-checker-fail-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-checker-fail")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-checker-fail",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        case_row = service._db_fetch_one(
            "SELECT runresult FROM judgehost_domjudge_cases WHERE id=?",
            [case_id],
        )
        self.assertIsNotNone(case_row)
        self.assertEqual(str(case_row["runresult"] or "").strip().lower(), "checker-fail")

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
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

    def test_domjudge_compare_exitcode_negative_with_hard_tl_is_tl(self) -> None:
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

        build_id = f"b-jh-compare-neg-tl-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-neg-tl-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-compare-neg-tl")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-compare-neg-tl",
            invocation_run_ids=[run_id],
            expected_behavior="wrong-answer-or-time-limit",
            invocation_source="run.execute",
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

        case_row = service._db_fetch_one(
            "SELECT runresult FROM judgehost_domjudge_cases WHERE id=?",
            [case_id],
        )
        self.assertIsNotNone(case_row)
        self.assertEqual(str(case_row["runresult"] or "").strip().lower(), "timelimit")

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "ok")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        first = tests[0] if tests else {}
        self.assertEqual(str((first or {}).get("verdict") or ""), "TL")
        self.assertEqual(str((first or {}).get("runresult") or "").strip().lower(), "timelimit")

    def test_domjudge_compare_script_internal_error_fails_whole_run(self) -> None:
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

        build_id = f"b-jh-compare-internal-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-internal-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-compare-internal")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-compare-internal",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        self.assertIn(
            "compare script 173 crashed with exit code 3, expected one of 42/43",
            str(summary.get("error") or ""),
        )

    def test_domjudge_internal_error_includes_debug_fail_message(self) -> None:
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

        build_id = f"b-jh-compare-debug-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-debug-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-compare-debug")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-compare-debug",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_internal_error_includes_payload_fail_message(self) -> None:
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

        build_id = f"b-jh-compare-payload-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-payload-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-compare-payload")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-compare-payload",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_internal_error_includes_job_level_debug_message(self) -> None:
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

        build_id = f"b-jh-compare-job-debug-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-job-debug-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests" / "002.in").write_text("ok\n", encoding="utf-8")
        (artifact_root / "ans" / "002.ans").write_text("ok\n", encoding="utf-8")
        service.domjudge_register_host("judgehost-compare-job-debug")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in", "002.in"],
            invocation_id="inv-compare-job-debug",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-compare-job-debug", max_batchsize=8)
        self.assertEqual(len(leased), 2)
        case_ids = sorted(int(item.get("judgetaskid") or 0) for item in leased)
        self.assertGreater(case_ids[0], 0)
        self.assertGreater(case_ids[1], 0)
        target_case = case_ids[1]
        row = service._db_fetch_one("SELECT job_id FROM judgehost_domjudge_cases WHERE id=?", [target_case])
        self.assertIsNotNone(row)
        job_id = int(row["job_id"])

        service.domjudge_update_judging(
            "judgehost-compare-job-debug",
            target_case,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )
        service.domjudge_add_debug_info(
            hostname="judgehost-compare-job-debug",
            judgetask_id=job_id,
            payload={"message": "FAIL compare script output from job-level debug"},
        )
        service.domjudge_internal_error(
            description="compare script 33 crashed with exit code 3, expected one of 42/43",
            judgetask_id=target_case,
        )

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("FAIL compare script output from job-level debug", error_text)

    def test_domjudge_internal_error_includes_judgehostlog_compare_output(self) -> None:
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

        build_id = f"b-jh-compare-jhlog-{uuid.uuid4().hex[:8]}"
        run_id = f"r-jh-compare-jhlog-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-compare-jhlog")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            invocation_id="inv-compare-jhlog",
            invocation_run_ids=[run_id],
            expected_behavior="accepted",
            invocation_source="run.execute",
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

        run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(run_row["summary_json"] or "{}"))
        error_text = str(summary.get("error") or "")
        self.assertIn("compare script 33 crashed with exit code 3, expected one of 42/43", error_text)
        self.assertIn("Comparing failed with exitcode 3, compare script output:", error_text)
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_fl_result_is_never_cached(self) -> None:
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

        build_id = f"b-jh-fl-cache-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-fl-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-fl-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)
        service.domjudge_register_host("judgehost-fl-cache")

        before_count = self._judge_index_entry_count(service.CASE_CACHE_KIND)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            invocation_id="inv-fl-cache-a",
            invocation_run_ids=[run_id_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
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
        failed_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_a])
        self.assertIsNotNone(failed_row)
        self.assertEqual(str(failed_row["status"] or ""), "failed")
        failed_summary = json.loads(str(failed_row["summary_json"] or "{}"))
        failed_tests = failed_summary.get("tests") if isinstance(failed_summary, dict) else []
        self.assertIsInstance(failed_tests, list)
        self.assertEqual(str((failed_tests[0] or {}).get("verdict") or ""), "FL")
        self.assertIn("001.in", str(failed_summary.get("error") or ""))
        self.assertIn("judge failed", str(failed_summary.get("error") or "").lower())

        after_fl_count = self._judge_index_entry_count(service.CASE_CACHE_KIND)
        self.assertEqual(after_fl_count, before_count)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            invocation_id="inv-fl-cache-b",
            invocation_run_ids=[run_id_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-fl-cache", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_a, case_id_b)

    def test_domjudge_force_recompile_bypasses_case_cache(self) -> None:
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

        build_id = f"b-jh-recompile-{uuid.uuid4().hex[:8]}"
        run_id_a = f"r-jh-recompile-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-recompile-b-{uuid.uuid4().hex[:8]}"
        self._seed_build(build_id)

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_a,
            selected_tests=["001.in"],
            invocation_id="inv-recompile-a",
            invocation_run_ids=[run_id_a],
            expected_behavior="accepted",
            invocation_source="run.execute",
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
        finished_a = config.db.fetch_one("SELECT status FROM runs WHERE id=?", [run_id_a])
        self.assertIsNotNone(finished_a)
        self.assertEqual(str(finished_a["status"] or ""), "ok")

        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            build_id=build_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id_b,
            selected_tests=["001.in"],
            invocation_id="inv-recompile-b",
            invocation_run_ids=[run_id_b],
            expected_behavior="accepted",
            invocation_source="run.execute",
            force_recompile=True,
        )
        tasks_b = service.domjudge_fetch_work("judgehost-recompile", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_a, case_id_b)

