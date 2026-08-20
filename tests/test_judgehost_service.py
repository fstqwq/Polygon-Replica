from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_execute,
    db_fetch_one,
    judgehost_cases_for_run,
    judgehost_fetch_case,
    judgehost_fetch_batch,
    verification_programs_for_tasks,
)

import base64
import io
import json
import os
import shutil
import threading
import time
import tarfile
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.service.verification.payload import prepared_payload_for_uploaded_source
from app.service.verification.plan import VerificationTestPlan
from app.service.judgehost.cache.executable import ExecutableCache
from app.service.judgehost.cache.case_result import CaseResultCache
from app.service.judgehost.domjudge.identity import job_id, submit_id
from app.service.judgehost.api import Judgehost
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.verification.diagnostic import compose_task_diagnostic_display
from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_scheduler import (
    VerificationRuntimeCallbacks,
    VerificationRuntimeCoordinator,
)
from app.service.verification.types import VerificationTaskStatus
from app.service.verification.judgehost_adapter import VerificationJudgehostAdapter
from tests.common import (
    E2ETestBase,
    runtime,
    configure_build_sources,
    configure_interactive_workspace,
    override_config_values,
)
from tests.identity_helpers import canonical_test_verification_id

_canonical_verification_id = canonical_test_verification_id
_GENERATOR_PROGRAM_ID = "generator-0"
_ACCEPTED_PROGRAM_ID = "accepted"
_SOLUTION_PROGRAM_ID = "solution-0"


def _pass_bundle_bytes(
    *,
    final_pass_number: int,
    historical_files: dict[int, dict[str, bytes]],
    final_input: bytes,
    final_team_message: bytes,
) -> bytes:
    entries: list[tuple[str, bytes]] = [
        (".polygon-pass-bundle", b""),
        ("final-pass-number", f"{final_pass_number}\n".encode("ascii")),
    ]
    for number, files in sorted(historical_files.items()):
        entries.extend(
            (f"passes/{number}/{name}", payload) for name, payload in files.items()
        )
    entries.extend(
        [
            (f"passes/{final_pass_number}/input", final_input),
            (
                f"passes/{final_pass_number}/teammessage.txt",
                final_team_message,
            ),
        ]
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


class TestJudgehostService(E2ETestBase):
    seed_default_workspace = True

    def test_unknown_judging_run_callback_is_idempotently_acknowledged(self) -> None:
        service = runtime.judgehost_task_service
        self.assertEqual(
            service.domjudge_add_judging_run(
                "judgehost-unknown-callback",
                987654,
                {},
            ),
            1,
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
                (case := judgehost_fetch_case(service, int(row["judgetaskid"])))
                is not None
                and str(case["task_id"]) == task_id
            )
        ]

    def _fresh_judgehost_service(self) -> Judgehost:
        service = Judgehost(
            runtime.workspace_service,
            runtime.config_values,
            execution_port=VerificationJudgehostAdapter(
                runtime.db,
                runtime.verification_task_store,
                runtime.verification_task_completion_service,
                runtime.verification_runtime_registry,
            ),
            runtime_blob_store=runtime.runtime_blob_store,
            runtime_cache_index=runtime.runtime_cache_index,
        )
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )
        return service

    def _verification_run_row(
        self, run_id: str, verification_id: str = ""
    ) -> dict[str, object] | None:
        safe_run_id = str(run_id or "").strip()
        if not safe_run_id:
            return None
        safe_verification_id = str(verification_id or "").strip()
        service = runtime.judgehost_task_service
        task_row = service.task_snapshot_for_run(safe_run_id)
        if task_row is not None and safe_verification_id:
            if str(task_row.get("verification_id") or "") != safe_verification_id:
                task_row = None
        if task_row is not None:
            row_verification_id = str(task_row.get("verification_id") or "")
            summary = service.run_summary(safe_run_id, row_verification_id)
            return {
                "status": str(task_row.get("run_status") or "").strip(),
                "summary": dict(summary),
                "verification_id": row_verification_id,
            }
        candidates = (
            [safe_verification_id] if safe_verification_id else [f"ver-{safe_run_id}"]
        )
        task_store = runtime.verification_task_store
        for candidate in candidates:
            token = str(candidate or "").strip()
            if not token:
                continue
            rows = task_store.list_rows(token)
            matched_rows = [
                row for row in rows if str(row["run_id"] or "") == safe_run_id
            ]
            if not matched_rows:
                continue
            tests = []
            for row in matched_rows:
                verdict = str(row["verdict"] or "")
                tests.append(
                    {
                        "test": str(row["test_name"] or ""),
                        "verdict": verdict,
                        "time_ms": int(
                            round(float(row["runtime_sec"] or 0.0) * 1000.0)
                        ),
                        "memory_kb": int(row["memory_kb"] or 0),
                        "message": str(row["feedback_text"] or row["error_text"] or ""),
                        "output_ref": str(row["output_ref"] or ""),
                        "feedback_files": [],
                        "passes": [
                            {
                                "index": 1,
                                "verdict": verdict,
                                "feedback": str(
                                    row["feedback_text"] or row["error_text"] or ""
                                ),
                                "output_ref": str(row["output_ref"] or ""),
                            }
                        ],
                    }
                )
            statuses = {str(row["status"] or "") for row in matched_rows}
            if statuses == {VerificationTaskStatus.DONE}:
                run_status = "ok"
            elif VerificationTaskStatus.FAILED in statuses:
                run_status = "failed"
            elif VerificationTaskStatus.CANCELLED in statuses:
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
        artifact_path = runtime.verification_service.artifact_path_for_verification(
            str(verification_id or "").strip()
        )
        if not artifact_path:
            raise AssertionError(
                f"missing artifact_path for verification: {verification_id}"
            )
        return Path(artifact_path).resolve()

    def test_domjudge_add_judging_run_survives_result_cache_publication_failure(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        ctx = runtime.workspace_service.workspace_context(
            self.problem, self.user, include_recent=False
        )
        verification_id = _canonical_verification_id(str(uuid.uuid4()))
        build_verification_id = _canonical_verification_id(f"build-{uuid.uuid4()}")
        verification_root = runtime.storage_layout.prepare_verification_root(
            verification_id
        ).resolve()
        verification_root.mkdir(parents=True, exist_ok=True)
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
        )
        self.assertEqual(admission.outcome, "admitted")

        run_id = f"r-immediate-finalize-{uuid.uuid4().hex[:8]}"
        task_id = verification_task_id(
            verification_id,
            _ACCEPTED_PROGRAM_ID,
            "001.in",
        )
        tasks = [
            PlannedTask(
                task_id=task_id,
                predecessor_task_id=None,
                task_kind="main-correct",
                source_path="solutions/ac.cpp",
                program_id=_ACCEPTED_PROGRAM_ID,
                test_name="001.in",
                expected_behavior="accepted",
            )
        ]
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
        )
        self.assertEqual(activation.outcome, "activated")
        self._seed_build_verification(
            build_verification_id,
            [("001.in", "ok\n", "ok\n")],
        )
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=build_verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=verification_id,
            verification_task_id=task_id,
            verification_program_id=_ACCEPTED_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            task_kind="main-correct",
            persist_verification_run=False,
        )
        task_store = runtime.verification_task_store

        callbacks = VerificationRuntimeCallbacks(
            publish_task=lambda _row: (_ for _ in ()).throw(
                RuntimeError("unexpected publish")
            ),
            probe_task_case_cache=lambda _task_ids: set(),
            cancel_execution=lambda _reason: None,
            close_programs=lambda program_ids: service.close_programs(
                verification_id,
                program_ids,
            ),
        )
        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=task_store,
            completion_service=runtime.verification_task_completion_service,
            callbacks=callbacks,
            edges=[],
        )
        runtime.verification_runtime_registry.register(verification_id, coordinator)
        coordinator_thread = threading.Thread(target=coordinator.run, daemon=True)
        coordinator_thread.start()
        try:
            service.domjudge_register_host("judgehost-immediate-finalize")
            leased = service.domjudge_fetch_work(
                "judgehost-immediate-finalize", max_batchsize=1
            )
            self.assertEqual(len(leased), 1)
            case_id = int(leased[0].get("judgetaskid") or 0)
            self.assertGreater(case_id, 0)
            task_store.set_task_leased(task_id)

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
            with patch.object(
                CaseResultCache,
                "try_store",
                side_effect=OSError("result cache unavailable"),
            ):
                ack = service.domjudge_add_judging_run(
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
            self.assertEqual(ack, 1)

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                rows = {
                    str(row["id"]): row for row in task_store.list_rows(verification_id)
                }
                if str(rows[task_id]["status"] or "") == VerificationTaskStatus.DONE:
                    break
                time.sleep(0.01)
            rows = {
                str(row["id"]): row for row in task_store.list_rows(verification_id)
            }
            self.assertEqual(
                str(rows[task_id]["status"] or ""),
                VerificationTaskStatus.DONE,
            )
            host = next(
                row
                for row in service.status()["hosts"]
                if row["hostname"] == "judgehost-immediate-finalize"
            )
            self.assertEqual(
                host["last_judging"],
                {
                    "verification_id": verification_id,
                    "problem_slug": self.problem,
                    "task_kind": "main-correct",
                    "source_label": "ac.cpp",
                    "test_name": "001.in",
                },
            )
        finally:
            runtime.verification_runtime_registry.unregister(
                verification_id,
                coordinator,
            )
            coordinator.enqueue_cancel("test shutdown")
            coordinator_thread.join(timeout=2.0)
            self.assertFalse(coordinator_thread.is_alive())

    def _assert_late_task_inherits_program_failure(
        self,
        *,
        failure_kind: str,
    ) -> None:
        service = self._fresh_judgehost_service()
        host = f"judgehost-late-{failure_kind}"
        service.domjudge_register_host(host)
        artifact_verification_id = _canonical_verification_id(
            f"build-late-{failure_kind}-{uuid.uuid4().hex[:8]}"
        )
        self._seed_build_verification(
            artifact_verification_id,
            [
                ("001.in", "first\n", "first\n"),
                ("002.in", "second\n", "second\n"),
            ],
        )

        ctx = runtime.workspace_service.workspace_context(
            self.problem,
            self.user,
            include_recent=False,
        )
        verification_id = _canonical_verification_id(
            f"late-{failure_kind}-{uuid.uuid4().hex[:8]}"
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
        )
        self.assertEqual(admission.outcome, "admitted")
        accepted_task_id = verification_task_id(
            verification_id,
            _ACCEPTED_PROGRAM_ID,
            "001.in",
        )
        late_task_id = verification_task_id(
            verification_id,
            _SOLUTION_PROGRAM_ID,
            "002.in",
        )
        tasks = [
            PlannedTask(
                task_id=accepted_task_id,
                predecessor_task_id=None,
                task_kind="main-correct",
                source_path="solutions/ac.cpp",
                program_id=_ACCEPTED_PROGRAM_ID,
                test_name="001.in",
                expected_behavior="accepted",
            ),
            PlannedTask(
                task_id=late_task_id,
                predecessor_task_id=accepted_task_id,
                task_kind="solution-run",
                source_path="solutions/ac.cpp",
                program_id=_SOLUTION_PROGRAM_ID,
                test_name="002.in",
                expected_behavior="unknown",
            ),
        ]
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
        )
        self.assertEqual(activation.outcome, "activated")
        runtime.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=accepted_task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=normalize_execution_result(verdict="OK"),
                ),
            )
        )

        first_run_id = f"r-first-{failure_kind}-{uuid.uuid4().hex[:8]}"
        service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=artifact_verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=first_run_id,
            selected_tests=["001.in"],
            verification_id=verification_id,
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="unknown",
            verification_source="run.execute",
            task_kind="solution-run",
            persist_verification_run=False,
        )
        leased = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(leased), 1)
        first_case_id = int(leased[0]["judgetaskid"])
        if failure_kind == "compiler-error":
            service.domjudge_update_judging(
                host,
                first_case_id,
                {
                    "compile_success": "0",
                    "output_compile": base64.b64encode(
                        b"fixture compile failure\n"
                    ).decode("ascii"),
                    "compile_metadata": "",
                },
            )
            expected_verdict = "CE"
            expected_task_status = VerificationTaskStatus.DONE
        elif failure_kind == "internal-error":
            service.domjudge_update_judging(
                host,
                first_case_id,
                {
                    "compile_success": "1",
                    "output_compile": "",
                    "compile_metadata": "",
                },
            )
            service.domjudge_internal_error(
                description="fixture internal error",
                judgetask_id=first_case_id,
            )
            expected_verdict = "FL"
            expected_task_status = VerificationTaskStatus.FAILED
        else:  # pragma: no cover - helper contract
            raise AssertionError(f"unknown failure kind: {failure_kind}")

        first_case = judgehost_fetch_case(service, first_case_id)
        self.assertIsNotNone(first_case)
        assert first_case is not None
        late_run_id = f"r-late-{failure_kind}-{uuid.uuid4().hex[:8]}"
        service.enqueue_task(
            verification_task_id=late_task_id,
            problem=self.problem,
            username=self.user,
            artifact_verification_id=artifact_verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=late_run_id,
            selected_tests=["002.in"],
            verification_id=verification_id,
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="unknown",
            verification_source="run.execute",
            task_kind="solution-run",
            persist_verification_run=False,
        )

        late_cases = judgehost_cases_for_run(service, late_run_id)
        self.assertEqual(len(late_cases), 1)
        late_case = late_cases[0]
        self.assertEqual(late_case["batch_id"], first_case["batch_id"])
        self.assertEqual(str(late_case["status"]), "reported")
        self.assertFalse(str(late_case["lease_owner"] or ""))
        late_report = service.poll_task_case_result(
            str(late_case["task_id"]),
            "002.in",
        )
        self.assertIsNotNone(late_report)
        assert late_report is not None
        self.assertEqual(
            str(late_report["status"]),
            "failed",
        )
        self.assertEqual(
            late_report["execution_result"].verdict,
            expected_verdict,
        )
        persisted = next(
            row
            for row in runtime.verification_task_store.list_rows(verification_id)
            if str(row["id"]) == late_task_id
        )
        self.assertEqual(
            str(persisted["status"]),
            expected_task_status,
        )
        self.assertEqual(str(persisted["verdict"]), expected_verdict)
        self.assertEqual(service.domjudge_fetch_work(host, max_batchsize=1), [])

    def test_late_task_inherits_persisted_compile_failure(self) -> None:
        self._assert_late_task_inherits_program_failure(failure_kind="compiler-error")

    def test_late_task_inherits_persisted_internal_error(self) -> None:
        self._assert_late_task_inherits_program_failure(failure_kind="internal-error")

    def _seed_build_verification(
        self,
        verification_id: str,
        items: list[tuple[str, str, str]] | None = None,
        *,
        run_config: dict[str, object] | None = None,
        include_run_config: bool = True,
    ) -> None:
        fixture_items = items or [("001.in", "ok\n", "ok\n")]
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "ac.cpp").write_text(
            "#include <bits/stdc++.h>\n"
            "using namespace std;\n"
            'int main(){string s; if(cin>>s) cout<<s<<"\\n"; return 0;}\n',
            encoding="utf-8",
        )
        ctx = runtime.workspace_service.workspace_context(
            self.problem, self.user, include_recent=False
        )
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        artifact_root = runtime.storage_layout.prepare_verification_root(
            verification_id
        ).resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
        )
        self.assertEqual(admission.outcome, "admitted")
        tasks: list[PlannedTask] = []
        completions: list[TaskCompletion] = []
        for index, (test_name, input_text, answer_text) in enumerate(fixture_items):
            task_id = verification_task_id(
                verification_id,
                _ACCEPTED_PROGRAM_ID,
                test_name,
            )
            tasks.append(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/ac.cpp",
                    program_id=_ACCEPTED_PROGRAM_ID,
                    test_name=test_name,
                    expected_behavior="accepted",
                )
            )
            input_ref = runtime.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name=test_name,
                role="input",
                file_name=test_name,
                payload=input_text.encode("utf-8"),
            )
            answer_ref = runtime.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name=test_name,
                role="answer",
                file_name=f"{Path(test_name).stem}.ans",
                payload=answer_text.encode("utf-8"),
            )
            completions.append(
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id=f"r-fixture-{verification_id[4:16]}-{index}",
                    judgehost_task_id="",
                    result=normalize_execution_result(verdict="OK"),
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                )
            )
        detail: dict[str, object] = {
            "selected_test_names": [item[0] for item in fixture_items],
        }
        if include_run_config:
            detail["run_config_json"] = json.dumps(
                run_config
                or {
                    "checker_mode": "testlib",
                    "pass_limit": 1,
                }
            )
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
            detail=detail,
        )
        self.assertEqual(activation.outcome, "activated")
        completion = runtime.verification_task_store.commit_task_completions(
            completions
        )
        self.assertEqual(completion.parent_transition, "ok")
        db_execute(
            "UPDATE verifications SET created_at=?, finished_at=? WHERE id=?",
            ["2026-02-28T00:00:00Z", "2026-02-28T00:00:00Z", verification_id],
        )

    def _judge_index_entry_count(self, kind: str) -> int:
        return int(runtime.runtime_cache_index.count_entries(namespace=kind))

    @staticmethod
    def _reset_task_queue_state(service) -> None:
        service.reset_runtime_state()

    def _lease_only_case(self, service, batch_id: int, hostname: str) -> int:
        rows = service.domjudge_fetch_work(hostname, max_batchsize=1)
        self.assertEqual(len(rows), 1)
        case = service.case_snapshot(int(rows[0]["judgetaskid"]))
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(int(case["batch_id"]), batch_id)
        return int(rows[0]["judgetaskid"])

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
        del test_name, verdict, feedback_files
        feedback = base64.b64encode(feedback_text.encode("utf-8")).decode("ascii")
        self.assertEqual(
            service.domjudge_add_judging_run(
                hostname,
                case_id,
                {
                    "runresult": runresult,
                    "runtime": "0.012",
                    "output_run": "",
                    "output_error": "",
                    "output_system": "",
                    "output_diff": feedback,
                    "metadata": base64.b64encode(
                        b"cpu-time: 0.011\nwall-time: 0.025\nmemory-bytes: 1437696\n"
                    ).decode("ascii"),
                    "compare_metadata": "",
                },
            ),
            1,
        )

    def test_draining_dispatches_work_admitted_before_drain(self) -> None:
        service = self._fresh_judgehost_service()
        self.addCleanup(service.reset_runtime_state)
        gate = MaintenanceAdmissionGate()
        service.set_admission_gate(gate)
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_FETCH_BATCH_SIZE=2,
        )
        verification_id = canonical_test_verification_id(
            f"b-jh-default-batch-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-default-batch-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        with gate.locked():
            gate.drain_locked()
        host = "judgehost-default-batch"
        service.domjudge_register_host(host)

        leader = service.domjudge_fetch_work(host)
        self.assertEqual(len(leader), 2)
        service.domjudge_update_judging(
            host,
            int(leader[0]["judgetaskid"]),
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        self.assertEqual(len(service.domjudge_fetch_work(host, max_batchsize=1)), 1)
        self.assertEqual(len(service.domjudge_fetch_work(host)), 1)

    def test_domjudge_endpoints_finalize_run(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-dom-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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

        source_files = service.domjudge_get_source_files(
            submit_id, contest_id=contest_id
        )
        self.assertTrue(source_files)
        self.assertEqual(source_files[0].filename, "ac.cpp")

        compile_files = service.domjudge_get_executable_files(
            "compile", compile_script_id
        )
        self.assertTrue(any(item.filename == "run" for item in compile_files))
        compile_run = next(
            (item for item in compile_files if item.filename == "run"), {}
        )
        compile_run_text = compile_run.payload.path.read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn(
            'exec g++ -x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE -I. "$MAIN" -o "$DEST"',
            compile_run_text,
        )

        testcase_files = service.domjudge_get_testcase_files(testcase_id)
        self.assertEqual(len(testcase_files), 2)
        self.assertEqual(
            {item.filename for item in testcase_files}, {"input", "output"}
        )
        case_row = judgehost_fetch_case(service, judgetask_id)
        self.assertIsNotNone(case_row)
        self.assertTrue(str(case_row["input_ref"] or "").startswith("blob://sha256/"))
        self.assertTrue(str(case_row["answer_ref"] or "").startswith("blob://sha256/"))

        service.domjudge_update_judging(
            "judgehost-official",
            judgetask_id,
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": "",
            },
        )

        meta_text = "time-used: cpu-time\ncpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
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
        feedback_files = (
            tests[0].get("feedback_files") if isinstance(tests[0], dict) else []
        )
        self.assertIsInstance(feedback_files, list)
        self.assertTrue(feedback_files)
        first_feedback_token = str(feedback_files[0] or "")
        self.assertTrue(first_feedback_token.startswith("blob://sha256/"))
        self.assertEqual(service.resolve_artifact_blob(first_feedback_token), b"ok\n")
        passes = tests[0].get("passes") if isinstance(tests[0], dict) else []
        self.assertIsInstance(passes, list)
        self.assertTrue(passes)
        first_pass = passes[0] if isinstance(passes[0], dict) else {}
        self.assertTrue(str(first_pass.get("feedback") or "").strip())
        host_rows = service.domjudge_list_hosts()
        self.assertTrue(host_rows)
        self.assertEqual(str(host_rows[0].get("hostname") or ""), "judgehost-official")

    def test_domjudge_executable_files_require_live_job_memory(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-script-provider-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        service.domjudge_register_host("judgehost-script-provider")
        leased = service.domjudge_fetch_work(
            "judgehost-script-provider", max_batchsize=8
        )
        task_row = next(iter(self._work_rows_for_task(service, leased, task_id)), None)
        self.assertIsNotNone(task_row)
        assert task_row is not None
        compare_script_id = int(task_row.get("compare_script_id") or 0)
        self.assertGreater(compare_script_id, 0)
        fresh_service = self._fresh_judgehost_service()
        with self.assertRaises(RuntimeError):
            fresh_service.domjudge_get_executable_files("compare", compare_script_id)

    def test_domjudge_executable_files_reuse_runtime_cache_entry(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-script-cache-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=True,
        )
        service.domjudge_register_host("judgehost-script-cache-a")
        leased_a = service.domjudge_fetch_work(
            "judgehost-script-cache-a", max_batchsize=8
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=True,
        )
        service.domjudge_register_host("judgehost-script-cache-b")
        leased_b: list[dict[str, object]] = []
        row_b = None
        for _ in range(8):
            leased_b = service.domjudge_fetch_work(
                "judgehost-script-cache-b", max_batchsize=8
            )
            row_b = next(
                iter(self._work_rows_for_task(service, leased_b, task_id_b)), None
            )
            if row_b is not None:
                break
        self.assertIsNotNone(row_b)
        assert row_b is not None
        compare_script_id_b = int(row_b.get("compare_script_id") or 0)
        self.assertEqual(compare_script_id_b, compare_script_id_a)

        compare_files = service.domjudge_get_executable_files(
            "compare",
            compare_script_id_b,
            hostname="judgehost-script-cache-b",
        )
        compare_names = {item.filename for item in compare_files}
        self.assertIn("run", compare_names)

    def test_domjudge_missing_executable_cache_fails_active_job(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-script-miss-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        task_cases = service.run_case_snapshots(run_id)
        self.assertTrue(task_cases)
        batch_row = service.batch_snapshot(int(task_cases[0]["batch_id"]))
        self.assertIsNotNone(batch_row)
        assert batch_row is not None
        with patch.object(ExecutableCache, "read", return_value=None):
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

    def test_generate_prepared_payload_recomputes_precomputed_from_final_verification_payload(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-generate-recompute-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-generate-recompute-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        validator_source = (
            '#include "testlib.h"\n'
            "int main(){\n"
            "  registerValidation();\n"
            "  inf.readInt();\n"
            "  inf.readEof();\n"
            "  return 0;\n"
            "}\n"
        ).encode("utf-8")
        input_file = runtime.runtime_blob_store.put_bytes(b'"$SUBMISSION_BIN" 4\n')
        answer_file = runtime.runtime_blob_store.put_bytes(b"")
        validator_file = runtime.runtime_blob_store.put_bytes(validator_source)
        testlib_file = runtime.runtime_blob_store.put_bytes(b"")
        prepared = prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id,
            test_name="001.in",
            input_file=input_file,
            answer_file=answer_file,
            verification_payload_base={
                "run_config_json": json.dumps(
                    {
                        "checker_mode": "testlib",
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
                "source_files": {
                    "validator.cpp": validator_file.to_payload(),
                    "testlib.h": testlib_file.to_payload(),
                },
            },
            extra_source_files={"testlib.h": testlib_file},
            manual_validate_only=False,
        )
        self.assertNotIn("precomputed", prepared)

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
            verification_program_id=_GENERATOR_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="verification.generate-input",
            task_kind="generate",
            bypass_case_result_cache=False,
            compile_only=False,
            persist_verification_run=False,
            prepared_payload=prepared,
        )
        service.domjudge_register_host("judgehost-generate-recompute")
        leased = service.domjudge_fetch_work(
            "judgehost-generate-recompute", max_batchsize=8
        )
        task_row = next(iter(self._work_rows_for_task(service, leased, task_id)), None)
        self.assertIsNotNone(task_row)
        assert task_row is not None
        compare_files = service.domjudge_get_executable_files(
            "compare", str(task_row.get("compare_script_id") or "")
        )
        compare_names = {item.filename for item in compare_files}
        self.assertIn("run", compare_names)
        self.assertIn("validator.cpp", compare_names)

    def test_domjudge_selected_tests_not_truncated_by_max_tests_per_task(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
            JUDGEHOST_MAX_TESTS_PER_TASK=1,
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-dom-notrunc-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-dom-notrunc-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="build.solve",
        )

        service.domjudge_register_host("judgehost-notrunc")
        rows = service.domjudge_fetch_work("judgehost-notrunc", max_batchsize=8)
        self.assertEqual(len(rows), 2)
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
            files = service.domjudge_get_testcase_files(testcase_id)
            self.assertEqual({item.filename for item in files}, {"input", "output"})
            input_text = next(
                item.payload.path.read_text(encoding="utf-8", errors="replace")
                for item in files
                if item.filename == "input"
            )
            inputs_seen.add(input_text)
        self.assertEqual(inputs_seen, {"ok\n", "second\n"})

    def test_domjudge_reuses_script_ids_for_same_hash_payload(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-dom-cache-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=True,
        )

        service.domjudge_register_host("judgehost-official-cache")

        rows_a = service.domjudge_fetch_work(
            "judgehost-official-cache", max_batchsize=8
        )
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
            rows_b = service.domjudge_fetch_work(
                "judgehost-official-cache", max_batchsize=8
            )
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

    def test_domjudge_multi_pass_summary_keeps_each_pass_and_raw_output(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-dom-mp-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-dom-mp-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            verification_id,
            run_config={
                "checker_mode": "testlib",
                "pass_limit": 2,
            },
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
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-mp"),
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        noisy_output = b"[  0.019s/6]>: 1 100\n" b"hello\n" b"[  0.054s/4]<: ? 0\n"
        meta_text = "cpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
        historical_meta = (
            b"time-used: cpu-time\ncpu-time: 0.003\n"
            b"wall-time: 0.006\nmemory-bytes: 8192\n"
        )
        bundle = _pass_bundle_bytes(
            final_pass_number=2,
            historical_files={
                1: {
                    "input": b"first input\n",
                    "program.out": b"first output\n",
                    "program.err": b"",
                    "system.out": b"first system\n",
                    "program.meta": historical_meta,
                    "compare.meta": b"exitcode: 42\n",
                    "judgemessage.txt": b"first ok\n",
                    "teammessage.txt": b"first team\n",
                }
            },
            final_input=b"second input\n",
            final_team_message=b"final team\n",
        )
        service.domjudge_add_judging_run(
            "judgehost-mp",
            judgetask_id,
            {
                "runresult": "correct",
                "runtime": "0.004",
                "output_run": base64.b64encode(noisy_output).decode("ascii"),
                "output_diff": base64.b64encode(b"first ok\nsecond ok\n").decode(
                    "ascii"
                ),
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": base64.b64encode(b"exitcode: 42\n").decode("ascii"),
                "team_message": base64.b64encode(bundle).decode("ascii"),
                "pass": "2",
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
        self.assertEqual(len(passes), 2)
        self.assertEqual(str((passes[0] or {}).get("verdict") or ""), "OK")
        first_output_ref = str((passes[0] or {}).get("output_ref") or "").strip()
        final_output_ref = str((passes[1] or {}).get("output_ref") or "").strip()
        first_judge_ref = str((passes[0] or {}).get("judge_message_ref") or "").strip()
        final_judge_ref = str((passes[1] or {}).get("judge_message_ref") or "").strip()
        self.assertEqual(
            service.resolve_artifact_blob(first_output_ref), b"first output\n"
        )
        self.assertEqual(service.resolve_artifact_blob(final_output_ref), noisy_output)
        self.assertEqual(service.resolve_artifact_blob(first_judge_ref), b"first ok\n")
        self.assertEqual(service.resolve_artifact_blob(final_judge_ref), b"second ok\n")
        run_root = self._verification_artifact_root(verification_id) / "runs" / run_id
        self.assertFalse((run_root / "001.out").exists())

    def test_domjudge_add_judging_run_rewrites_wa_to_tl_on_double_cpu(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-dom-wa2tl-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-dom-wa2tl-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            verification_id,
            run_config={
                "checker_mode": "testlib",
                "time_limit_ms": 6000,
            },
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
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-domjudge-wa2tl"),
            verification_program_id=_SOLUTION_PROGRAM_ID,
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

    def test_domjudge_register_host_requeues_leased_case_for_another_host(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-dom-reconnect-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        self.assertEqual(protocol_job_id, job_id(execution_verification_id))
        self.assertRegex(str(first.get("submitid") or ""), r"^[0-9]+$")
        self.assertRegex(str(first.get("uuid") or ""), r"^[0-9a-f]{64}$")
        self.assertEqual(int(first["submitid"]), submit_id(str(first["uuid"])))

        unfinished = service.domjudge_register_host("judgehost-reconnect")
        self.assertEqual(
            unfinished,
            [{"jobid": protocol_job_id, "submitid": str(first["submitid"])}],
        )

        case_id = int(first["judgetaskid"])
        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        assert case_row is not None
        self.assertEqual(str(case_row["lease_owner"] or ""), "")
        self.assertEqual(str(case_row["status"] or ""), "pending")

        self.assertEqual(service.domjudge_register_host("judgehost-reconnect"), [])
        second_rows = service.domjudge_fetch_work(
            "judgehost-reconnect-b", max_batchsize=8
        )
        self.assertEqual(len(second_rows), 1)
        self.assertEqual(int(second_rows[0]["judgetaskid"]), case_id)
        self.assertEqual(second_rows[0]["jobid"], first["jobid"])
        self.assertEqual(second_rows[0]["submitid"], first["submitid"])

        with self.assertRaisesRegex(
            RuntimeError,
            "judgehost does not own judging run",
        ):
            service.domjudge_update_judging(
                "judgehost-reconnect",
                case_id,
                {"compile_success": "0"},
            )
        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        assert case_row is not None
        batch_row = judgehost_fetch_batch(service, int(case_row["batch_id"]))
        self.assertIsNotNone(batch_row)
        assert batch_row is not None
        self.assertNotIn("compile_owner", batch_row)
        self.assertEqual(str(batch_row["status"] or ""), "open")
        self.assertEqual(str(batch_row["compile_state"] or ""), "unknown")
        self.assertEqual(str(case_row["lease_owner"] or ""), "judgehost-reconnect-b")
        self.assertEqual(str(case_row["status"] or ""), "leased")

        self._commit_case_result(
            service,
            case_id=case_id,
            hostname="judgehost-reconnect-b",
            test_name="001.in",
            runresult="correct",
            verdict="OK",
        )
        self.assertEqual(judgehost_fetch_case(service, case_id)["status"], "reported")

    def test_domjudge_valid_execution_events_refresh_host_heartbeat(self) -> None:
        service = runtime.judgehost_task_service
        verification_id = _canonical_verification_id(str(uuid.uuid4()))
        run_id = f"r-active-idle-heartbeat-{uuid.uuid4().hex[:8]}"
        host = "judgehost-active-idle-heartbeat"
        self._seed_build_verification(
            verification_id,
            [("001.in", "ok\n", "ok\n")],
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
            verification_id=_canonical_verification_id(str(uuid.uuid4())),
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertTrue(task_id.startswith("jt-"))

        service.domjudge_register_host(host)

        def _mark_host_stale() -> None:
            with patch(
                "app.service.judgehost.host.registry.now_iso",
                return_value="2000-01-01T00:00:00+00:00",
            ):
                service.record_host_peer_addr(host, "127.0.0.1")

        def _hostnames() -> set[str]:
            return {
                str(row.get("hostname") or "")
                for row in service.status().get("hosts", [])
            }

        def _host_row() -> dict[str, object]:
            rows = {
                str(row.get("hostname") or ""): row
                for row in service.status().get("hosts", [])
            }
            self.assertIn(host, rows)
            return rows[host]

        _mark_host_stale()
        first_rows = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(first_rows), 1)
        case_id = int(first_rows[0]["judgetaskid"])
        self.assertTrue(_host_row().get("online"))

        service.domjudge_check_versions(
            case_id,
            hostname=host,
            compiler=base64.b64encode(b"command=/usr/bin/g++\ng++ 14.2.0").decode(
                "ascii"
            ),
            runner="",
        )
        versions_row = _host_row()
        self.assertEqual(
            versions_row.get("toolchains"),
            [
                {
                    "language_id": "cpp",
                    "compiler": "command=/usr/bin/g++\ng++ 14.2.0",
                    "runner": "",
                    "observed_at": versions_row["toolchains"][0]["observed_at"],
                    "judgetask_id": case_id,
                }
            ],
        )

        service.domjudge_check_versions(
            case_id,
            hostname="judgehost-version-not-owner",
            compiler=base64.b64encode(b"spoofed compiler").decode("ascii"),
            runner="",
        )
        self.assertNotIn("judgehost-version-not-owner", _hostnames())

        _mark_host_stale()
        with self.assertRaisesRegex(
            RuntimeError,
            "judgehost does not own judging run",
        ):
            service.domjudge_update_judging(
                "judgehost-not-owner",
                case_id,
                {"compile_success": "1"},
            )
        self.assertFalse(_host_row().get("online"))
        self.assertNotIn("judgehost-not-owner", _hostnames())

        service.domjudge_update_judging(
            host,
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        self.assertTrue(_host_row().get("online"))

        _mark_host_stale()
        meta_text = "cpu-time: 0.004\nwall-time: 0.005\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            host,
            case_id,
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
        self.assertTrue(_host_row().get("online"))

        _mark_host_stale()
        second_rows = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(second_rows, [])
        self.assertTrue(_host_row().get("online"))

    def test_enqueue_rejects_invalid_payload_before_scheduling(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        build_bad = canonical_test_verification_id(
            f"b-jh-dom-bad-{uuid.uuid4().hex[:8]}"
        )
        run_bad = f"r-jh-dom-bad-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_bad)
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
                selected_tests=["999.in"],
                verification_id=_canonical_verification_id("inv-domjudge-bad"),
                verification_program_id=_SOLUTION_PROGRAM_ID,
                expected_behavior="accepted",
                verification_source="run.execute",
            )

        build_good = canonical_test_verification_id(
            f"b-jh-dom-good-{uuid.uuid4().hex[:8]}"
        )
        run_good = f"r-jh-dom-good-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(build_good)
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        service.domjudge_register_host("judgehost-skip-invalid")
        tasks = service.domjudge_fetch_work("judgehost-skip-invalid", max_batchsize=8)
        self.assertTrue(tasks)
        self.assertEqual(len(self._work_rows_for_task(service, tasks, good_task)), 1)

        bad_task_row = service.task_snapshot_for_run(run_bad)
        self.assertIsNotNone(bad_task_row)
        self.assertEqual(
            str(bad_task_row.get("status") or ""),
            VerificationTaskStatus.FAILED,
        )
        self.assertIn(
            "no tests in judgehost payload", str(bad_task_row.get("error_text") or "")
        )

    def test_domjudge_reuses_stable_testcase_identity_across_verifications(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        build_a = canonical_test_verification_id(f"b-jh-cache-a-{uuid.uuid4().hex[:8]}")
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        service.domjudge_register_host("judgehost-cache-a")
        rows_a = service.domjudge_fetch_work("judgehost-cache-a", max_batchsize=8)
        self.assertEqual(len(rows_a), 1)
        testcase_id_a = int(rows_a[0].get("testcase_id") or 0)
        case_id_a = int(rows_a[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_a, 0)
        self.assertGreater(testcase_id_a, 0)
        row_a = judgehost_fetch_case(service, case_id_a)
        self.assertIsNotNone(row_a)
        cached_testcase_id_a = int(row_a["testcase_id"] or 0)
        self.assertEqual(cached_testcase_id_a, testcase_id_a)

        build_b = canonical_test_verification_id(f"b-jh-cache-b-{uuid.uuid4().hex[:8]}")
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        service.domjudge_register_host("judgehost-cache-b")
        rows_b = service.domjudge_fetch_work("judgehost-cache-b", max_batchsize=8)
        self.assertEqual(len(rows_b), 1)
        testcase_id_b = int(rows_b[0].get("testcase_id") or 0)
        case_id_b = int(rows_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertGreater(testcase_id_b, 0)
        self.assertNotEqual(case_id_a, case_id_b)
        row_b = judgehost_fetch_case(service, case_id_b)
        self.assertIsNotNone(row_b)
        cached_testcase_id_b = int(row_b["testcase_id"] or 0)
        self.assertEqual(cached_testcase_id_b, testcase_id_b)
        self.assertEqual(cached_testcase_id_a, cached_testcase_id_b)

    def test_domjudge_testcase_files_resolve_by_stable_id_without_host_lease(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        build_a = canonical_test_verification_id(f"b-jh-host-a-{uuid.uuid4().hex[:8]}")
        run_a = f"r-jh-host-a-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            build_a,
            [("001.in", "alpha\n", "alpha-out\n")],
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        build_b = canonical_test_verification_id(f"b-jh-host-b-{uuid.uuid4().hex[:8]}")
        run_b = f"r-jh-host-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            build_b,
            [("001.in", "beta\n", "beta-out\n")],
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        self.assertGreater(testcase_id_a, 0)
        self.assertGreater(testcase_id_b, 0)
        self.assertNotEqual(testcase_id_a, testcase_id_b)

        files_a = service.domjudge_get_testcase_files(testcase_id_a)
        files_b = service.domjudge_get_testcase_files(testcase_id_b)
        input_a = next(
            item.payload.path.read_text(encoding="utf-8")
            for item in files_a
            if item.filename == "input"
        )
        input_b = next(
            item.payload.path.read_text(encoding="utf-8")
            for item in files_b
            if item.filename == "input"
        )
        self.assertEqual(input_a, "alpha\n")
        self.assertEqual(input_b, "beta\n")

    def test_domjudge_interactive_uses_configured_pass_limit(self) -> None:
        service = runtime.judgehost_task_service

        verification_id = canonical_test_verification_id(
            f"b-jh-passlimit-interactive-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-passlimit-interactive-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            verification_id,
            run_config={
                "checker_mode": "testlib",
                "pass_limit": 7,
            },
        )
        ws = Path(self._workspace_path())
        configure_interactive_workspace(
            ws,
            time_limit_ms=2000,
            memory_limit_mb=1024,
            pass_limit=7,
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        precomputed = payload.get("precomputed") if isinstance(payload, dict) else {}
        run_cfg = precomputed.get("run_config") if isinstance(precomputed, dict) else {}
        self.assertIsInstance(run_cfg, dict)
        self.assertEqual(int(run_cfg.get("pass_limit") or 0), 7)
        run_files = (
            precomputed.get("run_files") if isinstance(precomputed, dict) else []
        )
        self.assertIn("pass-capture", {item[0] for item in run_files})

    def test_domjudge_pass_fail_multi_pass_uses_configured_pass_limit(self) -> None:
        service = runtime.judgehost_task_service

        verification_id = canonical_test_verification_id(
            f"b-jh-passlimit-multipass-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-passlimit-multipass-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            verification_id,
            run_config={
                "checker_mode": "testlib",
                "pass_limit": 7,
            },
        )

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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        precomputed = payload.get("precomputed") if isinstance(payload, dict) else {}
        run_cfg = precomputed.get("run_config") if isinstance(precomputed, dict) else {}
        self.assertIsInstance(run_cfg, dict)
        self.assertEqual(int(run_cfg.get("pass_limit") or 0), 7)
        run_files = (
            precomputed.get("run_files") if isinstance(precomputed, dict) else []
        )
        compare_files = (
            precomputed.get("compare_files") if isinstance(precomputed, dict) else []
        )
        self.assertIn("pass-capture", {item[0] for item in run_files})
        self.assertIn("pass-capture", {item[0] for item in compare_files})

    def test_domjudge_interactor_source_overrides_host_binary_payload(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        ws = Path(self._workspace_path())
        configure_interactive_workspace(
            ws,
            time_limit_ms=2000,
            memory_limit_mb=1024,
            pass_limit=1,
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-interactor-source-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            compile_only=False,
        )

        host = "judgehost-interactor-source"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, tasks, task_id)), None)
        self.assertIsNotNone(task_row)

        run_files = service.domjudge_get_executable_files(
            "run", str(task_row.get("run_script_id") or "")
        )
        run_names = {item.filename for item in run_files}
        self.assertIn("build", run_names)
        self.assertIn("interactor.cpp", run_names)
        self.assertIn("testlib.h", run_names)
        self.assertNotIn("run", run_names)
        build_item = next((item for item in run_files if item.filename == "build"), {})
        build_text = build_item.payload.path.read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("-DDOMJUDGE", build_text)
        self.assertIn("interactor.cpp", build_text)
        pass_capture_item = next(
            item for item in run_files if item.filename == "pass-capture"
        )
        self.assertTrue(pass_capture_item.is_executable)

    def test_domjudge_java_upload_is_sent_with_detected_entry_point_filename(
        self,
    ) -> None:
        service = self._fresh_judgehost_service()
        verification_id = _canonical_verification_id(str(uuid.uuid4()))
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        source_files = service.domjudge_get_source_files(
            submit_id, contest_id=contest_id
        )
        self.assertTrue(source_files)
        self.assertEqual(source_files[0].filename, "TranslateMain.java")

    def test_domjudge_generate_verification_uses_generate_scripts_and_validator_payload(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-generate-scripts-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-generate-scripts-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        ws = Path(self._workspace_path())
        (ws / "validators").mkdir(parents=True, exist_ok=True)
        (ws / "validators" / "validator.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            encoding="utf-8",
        )
        configure_build_sources(
            ws,
            validator_source="validators/validator.cpp",
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
            verification_program_id=_GENERATOR_PROGRAM_ID,
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

        run_files = service.domjudge_get_executable_files(
            "run", str(task_row.get("run_script_id") or "")
        )
        run_item = next((item for item in run_files if item.filename == "run"), {})
        run_text = run_item.payload.path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("missing generate command payload", run_text)

        compare_files = service.domjudge_get_executable_files(
            "compare", str(task_row.get("compare_script_id") or "")
        )
        compare_run = next(
            (item for item in compare_files if item.filename == "run"), {}
        )
        compare_text = compare_run.payload.path.read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("VALIDATOR_BIN", compare_text)
        compare_names = {item.filename for item in compare_files}
        self.assertTrue(
            "validator" in compare_names or "validator.cpp" in compare_names
        )

    def test_domjudge_generate_verification_interactive_mode_does_not_require_interactor_payload(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-generate-interactive-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-generate-interactive-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        ws = Path(self._workspace_path())
        (ws / "validators").mkdir(parents=True, exist_ok=True)
        (ws / "validators" / "validator.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            encoding="utf-8",
        )
        (ws / "interactors").mkdir(parents=True, exist_ok=True)
        (ws / "interactors" / "interactor.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            encoding="utf-8",
        )
        configure_build_sources(
            ws,
            validator_source="validators/validator.cpp",
            interactor_source="interactors/interactor.cpp",
        )
        problem_path = ws / "config/problem.json"
        problem_config = json.loads(problem_path.read_text(encoding="utf-8"))
        problem_config["mode"] = "interactive"
        problem_path.write_text(
            json.dumps(problem_config, indent=2) + "\n",
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
            verification_program_id=_GENERATOR_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="build.generate-input",
            compile_only=False,
        )
        host = "judgehost-generate-interactive"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, tasks, task_id)), None)
        self.assertIsNotNone(task_row)

        run_files = service.domjudge_get_executable_files(
            "run", str(task_row.get("run_script_id") or "")
        )
        run_names = {item.filename for item in run_files}
        self.assertIn("run", run_names)
        self.assertNotIn("interactor.cpp", run_names)
        run_item = next((item for item in run_files if item.filename == "run"), {})
        run_text = run_item.payload.path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("missing generate command payload", run_text)

        compare_files = service.domjudge_get_executable_files(
            "compare", str(task_row.get("compare_script_id") or "")
        )
        compare_names = {item.filename for item in compare_files}
        self.assertTrue(
            "validator" in compare_names or "validator.cpp" in compare_names
        )

    def test_domjudge_compile_only_uses_single_virtual_case_even_with_build_tests(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compile-only-virtual-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            bypass_case_result_cache=True,
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-virtual")
        tasks = service.domjudge_fetch_work(
            "judgehost-compile-only-virtual", max_batchsize=16
        )
        task_rows = self._work_rows_for_task(service, tasks, task_id)
        self.assertEqual(len(task_rows), 1)
        case_id = int(task_rows[0].get("judgetaskid") or 0)
        testcase_id = int(task_rows[0].get("testcase_id") or 0)
        compare_script_id = str(task_rows[0].get("compare_script_id") or "")
        self.assertGreater(case_id, 0)
        self.assertGreater(testcase_id, 0)
        compare_files = service.domjudge_get_executable_files(
            "compare", compare_script_id
        )
        compare_run = next(
            (item for item in compare_files if item.filename == "run"), {}
        )
        compare_text = compare_run.payload.path.read_text(
            encoding="utf-8", errors="replace"
        )
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

    def test_domjudge_compile_only_multi_pass_with_interactor_stays_non_combined(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compile-only-multipass-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            bypass_case_result_cache=True,
            compile_only=True,
        )
        host = "judgehost-compile-only-multipass"
        service.domjudge_register_host(host)
        tasks = service.domjudge_fetch_work(host, max_batchsize=8)
        task_row = next(iter(self._work_rows_for_task(service, tasks, task_id)), None)
        self.assertIsNotNone(task_row)

        compare_cfg = json.loads(str(task_row.get("compare_config") or "{}"))
        self.assertFalse(bool(compare_cfg.get("combined_run_compare")))

        run_files = service.domjudge_get_executable_files(
            "run", str(task_row.get("run_script_id") or "")
        )
        run_item = next((item for item in run_files if item.filename == "run"), {})
        run_text = run_item.payload.path.read_text(encoding="utf-8", errors="replace")
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
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        host = "judgehost-compile-only-extra-cache"
        verification_id = canonical_test_verification_id(
            f"b-jh-compile-only-extra-cache-{uuid.uuid4().hex[:8]}"
        )
        self._seed_build_verification(verification_id)
        run_a = f"r-jh-compile-only-extra-a-{uuid.uuid4().hex[:8]}"
        extra_testlib = runtime.runtime_blob_store.put_bytes(b"// testlib\n")
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        prepared["extra_source_files"] = {"testlib.h": extra_testlib.to_payload()}

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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        original_lookup = CaseResultCache.lookup
        with patch.object(
            CaseResultCache,
            "lookup",
            autospec=True,
            side_effect=original_lookup,
        ) as cache_lookup:
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
                verification_id=_canonical_verification_id(
                    "inv-jh-compile-only-extra-b"
                ),
                verification_program_id=_SOLUTION_PROGRAM_ID,
                expected_behavior="compile",
                verification_source="build.compile",
                compile_only=True,
                prepared_payload=prepared,
            )
            rows_b = service.domjudge_fetch_work(host, max_batchsize=16)
            self.assertFalse(bool(self._work_rows_for_task(service, rows_b, task_b)))
            self.assertEqual(cache_lookup.call_count, 1)
        self.assertEqual(service.wait_for_task(task_b, timeout_sec=2.0), run_b)
        run_row_b = self._verification_run_row(run_b)
        self.assertIsNotNone(run_row_b)
        self.assertEqual(str(run_row_b["status"] or "").strip().lower(), "ok")
        self.assertEqual(run_row_b["summary"]["tests"], run_row_a["summary"]["tests"])

    def test_domjudge_compile_only_cache_hit_without_build_payload_tests(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

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
            verification_id=_canonical_verification_id(
                "inv-jh-compile-only-empty-build-a"
            ),
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
            verification_id=_canonical_verification_id(
                "inv-jh-compile-only-empty-build-b"
            ),
            verification_program_id=_SOLUTION_PROGRAM_ID,
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

    def test_domjudge_source_files_include_prepared_extra_sources(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )
        self._reset_task_queue_state(service)

        verification_id = canonical_test_verification_id(
            f"b-jh-extra-src-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-extra-src-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        extra_testlib = runtime.runtime_blob_store.put_bytes(b"// testlib helper\n")
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        prepared["extra_source_files"] = {"testlib.h": extra_testlib.to_payload()}

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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
            prepared_payload=prepared,
        )
        service.domjudge_register_host("judgehost-extra-src")
        work_rows = service.domjudge_fetch_work("judgehost-extra-src", max_batchsize=16)
        self.assertTrue(work_rows)
        work_row = next(
            iter(self._work_rows_for_task(service, work_rows, _task_id)), None
        )
        self.assertIsNotNone(work_row)
        submit_id = str(work_row.get("submitid") or "")
        contest_id = str(work_row.get("contestid") or "")
        source_files = service.domjudge_get_source_files(
            submit_id, contest_id=contest_id
        )
        names = {item.filename for item in source_files}
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

    def test_domjudge_compile_only_result_normalization_maps_success_to_ok(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compile-only-ok-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            bypass_case_result_cache=True,
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-ok")
        tasks = service.domjudge_fetch_work(
            "judgehost-compile-only-ok", max_batchsize=8
        )
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
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compile-only-no-output-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-no-output")
        tasks = service.domjudge_fetch_work(
            "judgehost-compile-only-no-output", max_batchsize=8
        )
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
        first_pass = passes[0] if isinstance(passes, list) and passes else {}
        self.assertFalse(str((first_pass or {}).get("output_ref") or "").strip())

    def test_domjudge_compile_only_result_normalization_maps_compile_failure_to_ce(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compile-only-ce-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="compile",
            verification_source="build.compile",
            compile_only=True,
        )
        service.domjudge_register_host("judgehost-compile-only-ce")
        tasks = service.domjudge_fetch_work(
            "judgehost-compile-only-ce", max_batchsize=8
        )
        self.assertEqual(len(tasks), 1)
        case_id = int(tasks[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        service.domjudge_update_judging(
            "judgehost-compile-only-ce",
            case_id,
            {
                "compile_success": "0",
                "output_compile": base64.b64encode(b"compile failed detail").decode(
                    "ascii"
                ),
                "compile_metadata": "",
            },
        )
        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        assert case_row is not None
        case_report = service.poll_task_case_result(
            task_id,
            str(case_row["test_name"]),
        )
        self.assertIsNotNone(case_report)
        assert case_report is not None
        canonical_result = case_report["execution_result"]
        self.assertIn("compile failed detail", canonical_result.compile.log)
        self.assertIn(
            "compile failed detail",
            str(canonical_result.compile.diagnostics[0]["message"]),
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
        diagnostics = (
            summary.get("compile_diagnostics") if isinstance(summary, dict) else []
        )
        self.assertIsInstance(diagnostics, list)
        first_diag = diagnostics[0] if diagnostics else {}
        self.assertIn(
            "compile failed detail", str((first_diag or {}).get("message") or "")
        )

    def test_domjudge_source_files_include_submission_extra_sources(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-extra-source-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_GENERATOR_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="build.generate-input",
            task_kind="generate",
            prepared_payload={
                "verification_payload": {
                    "tests": [
                        {
                            "name": "001.in",
                            "input_file": runtime.runtime_blob_store.put_bytes(
                                b"1\n"
                            ).to_payload(),
                            "answer_name": "001.ans",
                            "answer_file": runtime.runtime_blob_store.put_bytes(
                                b""
                            ).to_payload(),
                        }
                    ],
                    "run_config_json": json.dumps(
                        {
                            "checker_mode": "testlib",
                            "pass_limit": 1,
                            "time_limit_ms": 30000,
                            "memory_limit_mb": 1024,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "problem_limits": {
                        "time_limit_ms": 30000,
                        "memory_limit_mb": 1024,
                        "pass_limit": 1,
                    },
                    "source_files": {},
                },
                "extra_source_files": {
                    "testlib.h": runtime.runtime_blob_store.put_bytes(
                        b"// fake testlib\n"
                    ).to_payload(),
                },
            },
        )
        self.assertTrue(task_id)
        service.domjudge_register_host("judgehost-extra-source")
        tasks = service.domjudge_fetch_work("judgehost-extra-source", max_batchsize=8)
        self.assertEqual(len(tasks), 1)
        source_files = service.domjudge_get_source_files(
            str(tasks[0].get("submitid") or "")
        )
        source_names = {item.filename for item in source_files}
        self.assertIn("gen.cpp", source_names)
        self.assertIn("testlib.h", source_names)

    def test_domjudge_add_judging_run_endpoint_accepts_large_multipart_payload(
        self,
    ) -> None:
        from app.main import app

        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-large-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-large-{uuid.uuid4().hex[:8]}"
        large_output = b"A" * (2 * 1024 * 1024)
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
                verification_program_id=_SOLUTION_PROGRAM_ID,
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
                files={
                    "runresult": (None, "correct"),
                    "runtime": (None, "0.001"),
                    "output_run": (
                        None,
                        base64.b64encode(large_output).decode("ascii"),
                    ),
                    "output_diff": (
                        None,
                        base64.b64encode(b"validator accepted\n").decode("ascii"),
                    ),
                    "metadata": (
                        None,
                        base64.b64encode(metadata).decode("ascii"),
                    ),
                },
                headers=headers,
            )
            self.assertEqual(add_resp.status_code, 200)

            row = judgehost_fetch_case(service, case_id)
            self.assertIsNotNone(row)
            self.assertEqual(str(row["status"] or ""), "reported")
            self.assertEqual(str(row["runresult"] or ""), "correct")
            self.assertTrue(
                str(row["output_run_ref"] or "").startswith("blob://sha256/")
            )
            self.assertTrue(
                str(row["output_diff_ref"] or "").startswith("blob://sha256/")
            )
            self.assertTrue(str(row["metadata_ref"] or "").startswith("blob://sha256/"))
            self.assertEqual(
                runtime.runtime_blob_store.read(str(row["output_run_ref"])),
                large_output,
            )
            self.assertEqual(
                runtime.runtime_blob_store.read(str(row["output_diff_ref"])),
                b"validator accepted\n",
            )

    def test_judgehost_endpoints_reject_invalid_hostname_with_400(self) -> None:
        from app.main import app

        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        headers = {"Authorization": "Bearer test-token"}
        with TestClient(app) as client:
            register = client.post(
                "/api/v4/judgehosts",
                data={"hostname": "bad!host"},
                headers=headers,
            )
            callback = client.post(
                "/api/v4/judgehosts/add-judging-run/bad!host/987654",
                data={},
                headers=headers,
            )

        self.assertEqual(register.status_code, 400)
        self.assertEqual(callback.status_code, 400)

    def test_compile_update_rejects_noncanonical_blobs_without_state_change(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compile-invalid-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-compile-invalid-{uuid.uuid4().hex[:8]}"
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
            verification_id=_canonical_verification_id("inv-domjudge-compile-invalid"),
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        hostname = "judgehost-compile-invalid"
        service.domjudge_register_host(hostname)
        leased = service.domjudge_fetch_work(hostname, max_batchsize=1)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        assert case_row is not None
        batch_id = int(case_row["batch_id"])
        before = judgehost_fetch_batch(service, batch_id)
        self.assertIsNotNone(before)
        assert before is not None
        before_compile_state = (
            before["compile_success"],
            before["compile_state"],
            before["compile_output_b64"],
            before["compile_metadata_b64"],
        )

        def _last_seen_at() -> object:
            row = next(
                item
                for item in service.status().get("hosts", [])
                if item.get("hostname") == hostname
            )
            return row["last_seen_at"]

        before_last_seen_at = _last_seen_at()

        invalid_payloads = (
            {
                "compile_success": "1",
                "output_compile": 10**100,
                "compile_metadata": "",
            },
            {
                "compile_success": "1",
                "output_compile": "",
                "compile_metadata": True,
            },
            {
                "compile_success": "0",
                "output_compile": 10**100,
                "compile_metadata": "",
            },
            {
                "compile_success": "0",
                "output_compile": "",
                "compile_metadata": True,
            },
        )
        for invalid_payload in invalid_payloads:
            with (
                self.subTest(payload=invalid_payload),
                self.assertRaisesRegex(
                    RuntimeError,
                    "base64 text or raw bytes",
                ),
            ):
                service.domjudge_update_judging(
                    hostname,
                    case_id,
                    invalid_payload,
                )

            after = judgehost_fetch_batch(service, batch_id)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertEqual(
                (
                    after["compile_success"],
                    after["compile_state"],
                    after["compile_output_b64"],
                    after["compile_metadata_b64"],
                ),
                before_compile_state,
            )
            current_case = judgehost_fetch_case(service, case_id)
            self.assertIsNotNone(current_case)
            assert current_case is not None
            self.assertEqual(current_case["status"], "leased")
            self.assertEqual(_last_seen_at(), before_last_seen_at)

    def test_domjudge_compile_logs_are_truncated_before_state_storage(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compile-log-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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

        limit = int(runtime.config_values.JUDGEHOST_STORED_LOG_LIMIT_BYTES)
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
        stored_compile_output = base64.b64decode(
            str(batch_row["compile_output_b64"] or "")
        )
        stored_compile_metadata = base64.b64decode(
            str(batch_row["compile_metadata_b64"] or "")
        )
        self.assertLessEqual(len(stored_compile_output), limit)
        self.assertLessEqual(len(stored_compile_metadata), limit)
        self.assertIn(b"...[truncated]", stored_compile_output)
        self.assertIn(b"...[truncated]", stored_compile_metadata)

    def test_domjudge_fetch_work_endpoint_requires_hostname(self) -> None:
        from app.main import app

        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        hosts_before = [
            str(row.get("hostname") or "") for row in service.status().get("hosts", [])
        ]

        with TestClient(app) as client:
            resp = client.post(
                "/api/v4/judgehosts/fetch-work",
                data={"max_batchsize": "1"},
                headers={"Authorization": "Bearer test-token"},
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"detail": "hostname is required"})
        hosts_after = [
            str(row.get("hostname") or "") for row in service.status().get("hosts", [])
        ]
        self.assertEqual(hosts_after, hosts_before)
        self.assertNotIn("judgehost", hosts_after)

    def test_maintenance_returns_raw_503_but_fetch_work_keeps_returning_empty_200(
        self,
    ) -> None:
        from app.main import app

        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )
        gate = runtime.maintenance_admission_gate

        with TestClient(app) as client:
            with gate.locked():
                gate.close_locked()
            try:
                ordinary = client.get("/")
                self.assertEqual(ordinary.status_code, 503)
                self.assertEqual(ordinary.headers.get("retry-after"), "5")
                self.assertEqual(ordinary.headers.get("cache-control"), "no-store")
                self.assertIn("text/plain", ordinary.headers.get("content-type", ""))
                self.assertNotIn("<html", ordinary.text.lower())

                agent = client.get("/agent/v1/auth/status")
                self.assertEqual(agent.status_code, 503)
                self.assertEqual(agent.headers.get("retry-after"), "5")

                maintenance = client.get("/maintenance")
                self.assertEqual(maintenance.status_code, 200)
                self.assertIn("text/plain", maintenance.headers.get("content-type", ""))

                runtime_config = client.get(
                    "/api/v4/config",
                    headers={"Authorization": "Bearer test-token"},
                )
                self.assertEqual(runtime_config.status_code, 200, runtime_config.text)
                languages = client.get(
                    "/api/v4/languages",
                    headers={"Authorization": "Bearer test-token"},
                )
                self.assertEqual(languages.status_code, 200, languages.text)
                with patch.object(
                    service,
                    "domjudge_get_testcase_files",
                    side_effect=AssertionError(
                        "closed maintenance admission reached file lookup"
                    ),
                ) as get_testcase_files:
                    download = client.get(
                        "/api/v4/judgehosts/get_files/testcase/1",
                        headers={"Authorization": "Bearer test-token"},
                    )
                self.assertEqual(download.status_code, 503, download.text)
                self.assertEqual(download.headers.get("retry-after"), "5")
                get_testcase_files.assert_not_called()
                heartbeat = client.post(
                    "/api/v4/judgehosts",
                    data={"hostname": "judgehost-maintenance"},
                    headers={"Authorization": "Bearer test-token"},
                )
                self.assertEqual(heartbeat.status_code, 200, heartbeat.text)

                for _index in range(3):
                    fetch = client.post(
                        "/api/v4/judgehosts/fetch-work",
                        data={
                            "hostname": "judgehost-maintenance",
                            "max_batchsize": "1",
                        },
                        headers={"Authorization": "Bearer test-token"},
                    )
                    self.assertEqual(fetch.status_code, 200, fetch.text)
                    self.assertEqual(fetch.json(), [])
            finally:
                with gate.locked():
                    gate.open_locked()

    def test_file_stream_holds_maintenance_admission_until_body_closes(self) -> None:
        from app.main import app

        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )
        stream_started = threading.Event()
        release_stream = threading.Event()
        responses: list[object] = []
        failures: list[Exception] = []

        def blocking_stream(_rows):
            stream_started.set()
            yield b"["
            release_stream.wait(timeout=2)
            yield b"]"

        def request_download(client: TestClient) -> None:
            try:
                responses.append(
                    client.get(
                        "/api/v4/judgehosts/get_files/source/1",
                        headers={"Authorization": "Bearer test-token"},
                    )
                )
            except Exception as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        with (
            patch.object(service, "domjudge_get_source_files", return_value=[]),
            patch(
                "app.impl.judgehost.api.stream_domjudge_file_array",
                side_effect=blocking_stream,
            ),
            TestClient(app) as client,
        ):
            request_thread = threading.Thread(
                target=request_download,
                args=(client,),
            )
            request_thread.start()
            try:
                self.assertTrue(stream_started.wait(timeout=2))
                self.assertEqual(service.busy_counts()["callbacks"], 1)
                drained = runtime.maintenance_service.begin_drain()
                self.assertTrue(drained.accepted, drained.reason)
                started = runtime.maintenance_service.start_cleanup(
                    actor_user_id=0,
                )
                self.assertFalse(started.accepted)
                self.assertEqual(started.reason, "busy")
                self.assertEqual(started.busy["judgehost_callbacks"], 1)
            finally:
                release_stream.set()
                request_thread.join(timeout=2)
                runtime.maintenance_service.cancel_drain()

        self.assertFalse(request_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(responses), 1)
        self.assertEqual(getattr(responses[0], "status_code", None), 200)
        self.assertEqual(service.busy_counts()["callbacks"], 0)

    def test_fetch_work_long_poll_does_not_hold_maintenance_admission_lock(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        gate = runtime.maintenance_admission_gate
        waiting = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        result: list[list[dict[str, object]]] = []

        def wait_without_holding_admission(_timeout_sec: float) -> bool:
            waiting.set()
            release.wait(timeout=2)
            return False

        with patch(
            "app.service.judgehost.batch.runtime.JudgehostBatchRuntime.wait_for_ready_batch",
            side_effect=wait_without_holding_admission,
        ):
            with gate.locked():
                gate.open_locked()
            fetch_thread = threading.Thread(
                target=lambda: result.append(
                    service.domjudge_fetch_work(
                        "judgehost-maintenance-long-poll",
                        max_batchsize=1,
                    )
                )
            )
            fetch_thread.start()
            self.assertTrue(waiting.wait(timeout=2))

            def close_admission() -> None:
                with gate.locked():
                    gate.close_locked()
                    closed.set()

            close_thread = threading.Thread(target=close_admission)
            close_thread.start()
            try:
                self.assertTrue(closed.wait(timeout=1))
                release.set()
                fetch_thread.join(timeout=2)
                self.assertFalse(fetch_thread.is_alive())
                self.assertEqual(result, [[]])
            finally:
                release.set()
                fetch_thread.join(timeout=2)
                close_thread.join(timeout=2)
                with gate.locked():
                    gate.open_locked()

    def test_draining_empty_fetch_work_skips_long_poll(self) -> None:
        service = runtime.judgehost_task_service
        gate = runtime.maintenance_admission_gate
        with gate.locked():
            gate.drain_locked()
        try:
            with patch(
                "app.service.judgehost.batch.runtime.JudgehostBatchRuntime.wait_for_ready_batch",
                side_effect=AssertionError("draining fetch-work must not long-poll"),
            ):
                started = time.monotonic()
                result = service.domjudge_fetch_work(
                    "judgehost-maintenance-draining",
                    max_batchsize=1,
                )
            self.assertEqual(result, [])
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            with gate.locked():
                gate.open_locked()

    def test_fetch_work_does_not_wait_for_busy_maintenance_admission_lock(self) -> None:
        service = runtime.judgehost_task_service
        gate = runtime.maintenance_admission_gate
        locked = threading.Event()
        release = threading.Event()

        def hold_admission() -> None:
            with gate.locked():
                gate.open_locked()
                locked.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_admission)
        holder.start()
        try:
            self.assertTrue(locked.wait(timeout=1))
            started = time.monotonic()
            result = service.domjudge_fetch_work(
                "judgehost-maintenance-busy-lock",
                max_batchsize=1,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result, [])
            self.assertLess(elapsed, 0.5)
        finally:
            release.set()
            holder.join(timeout=2)
            with gate.locked():
                gate.open_locked()

    def test_domjudge_testcase_files_endpoint_allows_authenticated_no_peer_access(
        self,
    ) -> None:
        from app.main import app

        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        with TestClient(app) as client:
            verification_id = canonical_test_verification_id(
                f"b-jh-peer-{uuid.uuid4().hex[:8]}"
            )
            run_id = f"r-jh-peer-{uuid.uuid4().hex[:8]}"
            self._seed_build_verification(
                verification_id,
                [("001.in", "peer-input\n", "peer-output\n")],
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
                selected_tests=["001.in"],
                verification_id=_canonical_verification_id("inv-domjudge-peer"),
                verification_program_id=_SOLUTION_PROGRAM_ID,
                expected_behavior="accepted",
                verification_source="run.execute",
            )
            service.domjudge_register_host("judgehost-no-peer")
            tasks = service.domjudge_fetch_work("judgehost-no-peer", max_batchsize=1)
            self.assertEqual(len(tasks), 1)
            testcase_id = int(tasks[0].get("testcase_id") or 0)
            submit_id = str(tasks[0].get("submitid") or "")
            contest_id = str(tasks[0].get("contestid") or "")
            self.assertGreater(testcase_id, 0)
            testcase_resp = client.get(
                f"/api/v4/judgehosts/get_files/testcase/{testcase_id}",
                headers={
                    "Authorization": "Bearer test-token",
                    "X-Forwarded-For": "203.0.113.44",
                },
            )
            source_resp_90 = client.get(
                f"/api/v4/judgehosts/get_files/source/{submit_id}",
                headers={"Authorization": "Bearer test-token"},
            )
            source_resp_main = client.get(
                f"/api/v4/judgehosts/get_files/source/{contest_id}/{submit_id}",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(testcase_resp.status_code, 200)
        self.assertEqual(source_resp_90.status_code, 200)
        self.assertEqual(source_resp_main.status_code, 200)
        self.assertEqual(source_resp_90.json()[0]["filename"], "ac.cpp")
        self.assertEqual(source_resp_main.json()[0]["filename"], "ac.cpp")
        files = testcase_resp.json()
        input_blob = next(
            str(item.get("content") or "")
            for item in files
            if str(item.get("filename") or "") == "input"
        )
        self.assertEqual(
            base64.b64decode(input_blob).decode("utf-8", errors="replace"),
            "peer-input\n",
        )

    def test_domjudge_executable_files_endpoint_allows_authenticated_no_peer_access(
        self,
    ) -> None:
        from app.main import app

        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        with TestClient(app) as client:
            verification_id = canonical_test_verification_id(
                f"b-jh-peer-script-{uuid.uuid4().hex[:8]}"
            )
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
                verification_program_id=_SOLUTION_PROGRAM_ID,
                expected_behavior="accepted",
                verification_source="run.execute",
            )
            service.domjudge_register_host("judgehost-no-peer-script")
            tasks = service.domjudge_fetch_work(
                "judgehost-no-peer-script", max_batchsize=1
            )
            self.assertEqual(len(tasks), 1)
            compare_script_id = int(tasks[0].get("compare_script_id") or 0)
            self.assertGreater(compare_script_id, 0)
            compare_resp = client.get(
                f"/api/v4/judgehosts/get_files/compare/{compare_script_id}",
                headers={
                    "Authorization": "Bearer test-token",
                    "X-Forwarded-For": "198.51.100.27",
                },
            )
        self.assertEqual(compare_resp.status_code, 200)
        files = compare_resp.json()
        filenames = {str(item.get("filename") or "") for item in files}
        self.assertIn("run", filenames)

    def test_domjudge_oversized_plain_multipart_fails_with_a_durable_diagnostic(
        self,
    ) -> None:
        from app.main import app

        with TestClient(app) as client:
            service = runtime.judgehost_task_service
            override_config_values(
                self,
                runtime.config_values,
                JUDGEHOST_ENABLE=True,
                JUDGEHOST_API_TOKEN="test-token",
                JUDGEHOST_API_USERNAME="judgehost",
            )
            verification_id = canonical_test_verification_id(
                f"b-jh-spool-{uuid.uuid4().hex[:8]}"
            )
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
                verification_program_id=_SOLUTION_PROGRAM_ID,
                expected_behavior="accepted",
                verification_source="run.execute",
            )
            service.domjudge_register_host("judgehost-spool")
            tasks = service.domjudge_fetch_work("judgehost-spool", max_batchsize=1)
            self.assertEqual(len(tasks), 1)
            case_id = int(tasks[0].get("judgetaskid") or 0)
            self.assertGreater(case_id, 0)

            oversized_output = base64.b64encode(b"B" * 2048).decode("ascii")
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

            with patch(
                "app.impl.judgehost.api._judgehost_form_part_limit_bytes",
                return_value=1024,
            ):
                add_resp = client.post(
                    f"/api/v4/judgehosts/add-judging-run/judgehost-spool/{case_id}",
                    files={
                        "runresult": (None, "correct"),
                        "output_run": (None, oversized_output),
                    },
                    headers=headers,
                )
            self.assertEqual(add_resp.status_code, 413)

            run_row = self._verification_run_row(run_id)
            self.assertIsNotNone(run_row)
            self.assertEqual(str(run_row["status"] or ""), "failed")
            error_text = str(dict(run_row["summary"]).get("error") or "")
            self.assertIn("Judgehost result callback was rejected", error_text)
            self.assertIn("configured 1024-byte limit", error_text)
            self.assertNotIn("failed without Judgehost diagnostics", error_text)

    def test_domjudge_build_solve_uses_problem_limits_when_run_config_missing(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        ws = Path(self._workspace_path())
        configure_interactive_workspace(
            ws,
            time_limit_ms=6000,
            memory_limit_mb=1,
            pass_limit=1,
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-limits-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-limits-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            verification_id,
            include_run_config=False,
        )
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
            verification_id=verification_id,
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        self.assertAlmostEqual(
            float(run_config.get("time_limit") or 0.0), 6.0, places=3
        )
        self.assertAlmostEqual(float(run_config.get("overshoot") or 0.0), 0.0, places=3)
        self.assertEqual(int(run_config.get("memory_limit") or 0), 1024)
        self.assertEqual(int(run_config.get("process_limit") or 0), 1024)
        self.assertEqual(
            int(run_config.get("output_limit") or 0),
            int(runtime.config_values.UPLOAD_MAX_BYTES) // 1024,
        )
        self.assertEqual(int(run_config.get("pass_limit") or 0), 1)
        compile_output_kb = int(runtime.config_values.TOOLCHAIN_COMPILE_OUTPUT_KB)
        aux_limit_bytes = int(runtime.config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES)
        self.assertEqual(
            int(compare_config.get("script_filesize_limit") or 0),
            compile_output_kb,
        )
        self.assertEqual(
            int(compare_config.get("script_memory_limit") or 0),
            int(runtime.config_values.TOOLCHAIN_COMPILE_MEMORY_MB) * 1024,
        )
        self.assertEqual(
            int(compile_config.get("script_filesize_limit") or 0),
            compile_output_kb,
        )
        self.assertGreaterEqual(
            int(compare_config.get("script_filesize_limit") or 0), 1024
        )
        self.assertGreaterEqual(
            int(compile_config.get("script_filesize_limit") or 0), 1024
        )
        self.assertNotEqual(
            int(compare_config.get("script_filesize_limit") or 0),
            (aux_limit_bytes + 1023) // 1024,
        )
        self.assertNotEqual(
            int(compile_config.get("script_filesize_limit") or 0),
            (aux_limit_bytes + 1023) // 1024,
        )

    def test_domjudge_compare_config_uses_compile_memory_when_checker_source_compiles_during_compare(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        ws = Path(self._workspace_path())
        (ws / "checkers").mkdir(parents=True, exist_ok=True)
        (ws / "checkers" / "checker.cpp").write_text(
            "#include <bits/stdc++.h>\nint main(int, char**){return 0;}\n",
            encoding="utf-8",
        )
        configure_build_sources(ws, checker_source="checkers/checker.cpp")

        compile_mem_mb = max(
            64,
            int(runtime.config_values.TOOLCHAIN_COMPILE_MEMORY_MB),
        )
        run_mem_mb = compile_mem_mb + 1024

        verification_id = canonical_test_verification_id(
            f"b-jh-compare-compile-mem-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-compare-compile-mem-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            verification_id,
            run_config={
                "checker_mode": "testlib",
                "pass_limit": 1,
                "memory_limit_mb": run_mem_mb,
            },
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
            selected_tests=["001.in"],
            verification_id=verification_id,
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        compare_names = {item.filename for item in compare_files}

        self.assertIn("checker.cpp", compare_names)
        self.assertEqual(int(run_config.get("memory_limit") or 0), run_mem_mb * 1024)
        self.assertGreater(
            int(run_config.get("memory_limit") or 0), compile_mem_mb * 1024
        )
        self.assertEqual(
            int(compare_config.get("script_memory_limit") or 0),
            max(int(run_config.get("memory_limit") or 0), compile_mem_mb * 1024),
        )

    def test_domjudge_main_correct_includes_checker_files_in_compare_payload(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        ws = Path(self._workspace_path())
        (ws / "checkers").mkdir(parents=True, exist_ok=True)
        (ws / "third_party" / "testlib").mkdir(parents=True, exist_ok=True)
        (ws / "checkers" / "checker.cpp").write_text(
            '#include "testlib.h"\nint main(int argc, char** argv){registerTestlibCmd(argc, argv); quitf(_ok, "ok");}\n',
            encoding="utf-8",
        )
        (ws / "third_party" / "testlib" / "testlib.h").write_text(
            "#pragma once\n#define _ok 0\ninline void registerTestlibCmd(int, char**){ }\ninline void quitf(int, const char*, ...){ }\n",
            encoding="utf-8",
        )
        configure_build_sources(ws, checker_source="checkers/checker.cpp")

        verification_id = canonical_test_verification_id(
            f"b-jh-main-correct-{uuid.uuid4().hex[:8]}"
        )
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
            verification_id=verification_id,
            verification_program_id=_ACCEPTED_PROGRAM_ID,
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
        compare_names = {item.filename for item in compare_files}

        self.assertIn("run", compare_names)
        self.assertIn("checker.cpp", compare_names)
        self.assertIn("testlib.h", compare_names)

    def test_domjudge_build_solve_defaults_pass_limit_from_problem_config(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        ws = Path(self._workspace_path())
        configure_interactive_workspace(
            ws,
            time_limit_ms=2000,
            memory_limit_mb=1024,
            pass_limit=3,
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-multipass-default-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-multipass-default-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            verification_id,
            include_run_config=False,
        )

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
            verification_id=verification_id,
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="build.solve",
        )

        service.domjudge_register_host("judgehost-multipass-default")
        tasks = service.domjudge_fetch_work(
            "judgehost-multipass-default", max_batchsize=1
        )
        self.assertEqual(len(tasks), 1)
        run_config_raw = str(tasks[0].get("run_config") or "{}")
        run_config = json.loads(run_config_raw)
        self.assertEqual(int(run_config.get("pass_limit") or 0), 3)

    def test_domjudge_active_cache_probe_finishes_hits_and_leases_only_misses(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-partial-cache-{uuid.uuid4().hex[:8]}"
        )
        run_id_seed = f"r-jh-partial-seed-{uuid.uuid4().hex[:8]}"
        run_id_hit = f"r-jh-full-hit-{uuid.uuid4().hex[:8]}"
        run_id_target = f"r-jh-partial-target-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        seed_tasks = service.domjudge_fetch_work(
            "judgehost-partial-cache", max_batchsize=8
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertEqual(service.probe_task_case_cache([hit_task_id]), set())
        self.assertEqual(
            service.wait_for_task(hit_task_id, timeout_sec=2.0), run_id_hit
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )

        rows = judgehost_cases_for_run(service, run_id_target)
        self.assertEqual(len(rows), 2)
        self.assertEqual({str(row["status"]) for row in rows}, {"cache-pending"})

        self.assertEqual(service.probe_task_case_cache([target_task_id]), set())
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

    def test_domjudge_expected_accepted_does_not_shortcut_non_ok_cache(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-accepted-cache-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_a = service.domjudge_fetch_work(
            "judgehost-accepted-cache", max_batchsize=8
        )
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
        failed_tests = (
            failed_summary.get("tests") if isinstance(failed_summary, dict) else []
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work(
            "judgehost-accepted-cache", max_batchsize=8
        )
        self.assertEqual(len(tasks_b), 1)
        self.assertGreater(int(tasks_b[0].get("judgetaskid") or 0), 0)

    def test_domjudge_compare_exitcode_3_is_tagged_checker_fail(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-checker-fail-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
                "compare_metadata": base64.b64encode(
                    b"exitcode: 3\ncpu-time: 0.001\n"
                ).decode("ascii"),
            },
        )

        case_row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(case_row)
        self.assertEqual(
            str(case_row["runresult"] or "").strip().lower(), "checker-fail"
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        self.assertIn("001.in: comparing failed", str(summary.get("error") or ""))
        tests = summary.get("tests") if isinstance(summary, dict) else []
        self.assertIsInstance(tests, list)
        first = tests[0] if tests else {}
        self.assertEqual(str((first or {}).get("verdict") or ""), "FL")
        self.assertEqual(
            str((first or {}).get("runresult") or "").strip().lower(), "checker-fail"
        )
        passes = (first or {}).get("passes") if isinstance(first, dict) else []
        pass_row = passes[0] if isinstance(passes, list) and passes else {}
        self.assertEqual(
            str((pass_row or {}).get("runresult") or "").strip().lower(), "checker-fail"
        )
        self.assertIn("comparing failed", str((pass_row or {}).get("feedback") or ""))

    def test_domjudge_run_error_prefers_program_stderr_feedback(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-run-error-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
                "output_diff": base64.b64encode(b"judge fallback message\n").decode(
                    "ascii"
                ),
                "output_error": base64.b64encode(stderr_text.encode("utf-8")).decode(
                    "ascii"
                ),
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
        feedback_files = (
            (first or {}).get("feedback_files") if isinstance(first, dict) else []
        )
        self.assertIsInstance(feedback_files, list)
        self.assertTrue(feedback_files)
        self.assertTrue(str(feedback_files[0] or "").startswith("blob://sha256/"))
        self.assertEqual(
            service.resolve_artifact_blob(str(feedback_files[0])),
            stderr_text.encode("utf-8"),
        )
        passes = (first or {}).get("passes") if isinstance(first, dict) else []
        pass_row = passes[0] if isinstance(passes, list) and passes else {}
        self.assertIn(
            "terminate called after throwing",
            str((pass_row or {}).get("feedback") or ""),
        )
        self.assertNotIn(
            "judge fallback message", str((pass_row or {}).get("feedback") or "")
        )

    def test_domjudge_compare_exitcode_negative_with_hard_tl_is_tl(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compare-neg-tl-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="wrong-answer-or-time-limit",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work(
            "judgehost-compare-neg-tl", max_batchsize=8
        )
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
        self.assertEqual(
            str((first or {}).get("runresult") or "").strip().lower(), "timelimit"
        )

    def test_domjudge_compare_script_internal_error_fails_whole_run(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compare-internal-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work(
            "judgehost-compare-internal", max_batchsize=8
        )
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
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compare-debug-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        self.assertIn(
            "compare script 33 crashed with exit code 3, expected one of 42/43",
            error_text,
        )
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_internal_error_includes_payload_fail_message(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compare-payload-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work(
            "judgehost-compare-payload", max_batchsize=8
        )
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
        self.assertIn(
            "compare script 33 crashed with exit code 3, expected one of 42/43",
            error_text,
        )
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_batch_id_is_not_a_judgetask_id(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-case-only-id-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-case-only-id-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-case-only-id")

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
            verification_id=_canonical_verification_id("inv-case-only-id"),
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-case-only-id", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0]["judgetaskid"])
        row = judgehost_fetch_case(service, case_id)
        self.assertIsNotNone(row)
        assert row is not None
        batch_id = int(row["batch_id"])
        before = judgehost_fetch_batch(service, batch_id)
        self.assertIsNotNone(before)

        service.domjudge_add_debug_info(
            hostname="judgehost-case-only-id",
            judgetask_id=batch_id,
            payload={"message": "FAIL compare script output from batch-level debug"},
        )
        self.assertEqual(
            service.domjudge_internal_error(
                description="must not target a batch",
                judgetask_id=batch_id,
            ),
            0,
        )

        after = judgehost_fetch_batch(service, batch_id)
        self.assertEqual(after, before)
        self.assertEqual(judgehost_fetch_case(service, case_id)["status"], "leased")

    def test_domjudge_internal_error_includes_judgehostlog_compare_output(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compare-jhlog-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
            payload={
                "judgehostlog": base64.b64encode(judgehost_log.encode("utf-8")).decode(
                    "ascii"
                )
            },
        )

        run_row = self._verification_run_row(run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").strip().lower(), "failed")
        summary = dict(run_row["summary"])
        error_text = str(summary.get("error") or "")
        self.assertIn(
            "compare script 33 crashed with exit code 3, expected one of 42/43",
            error_text,
        )
        self.assertIn(
            "Comparing failed with exitcode 3, compare script output:", error_text
        )
        self.assertIn("FAIL Can not write to the result file (test case 1)", error_text)

    def test_domjudge_internal_error_strips_raw_base64_payload_blob(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-compare-strip-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        description = (
            "compare script 33 crashed with exit code 3, expected one of 42/43"
        )
        judgehost_log = (
            "[Mar 22 20:27:50.752] testcase_run.sh[18759]: Comparing failed with exitcode 3, compare script output:\n"
            'Expected integer, but ""$SUBMISSION_BIN"" found (test case 1, testdata.in)\n'
        )
        judgehost_log_b64 = base64.b64encode(judgehost_log.encode("utf-8")).decode(
            "ascii"
        )
        service.domjudge_internal_error(
            description=description,
            judgetask_id=case_id,
            payload={
                "description": description,
                "judgehostlog": judgehost_log_b64,
                "disabled": '{"kind":"compare_script","compare_script_id":"33"}',
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
        self.assertIn(
            "Comparing failed with exitcode 3, compare script output:", error_text
        )
        self.assertIn('Expected integer, but ""$SUBMISSION_BIN"" found', error_text)
        self.assertNotIn(judgehost_log_b64, error_text)
        self.assertNotIn('"judgehostlog":', error_text)
        self.assertNotIn('"disabled":', error_text)

    def test_domjudge_fl_result_is_persisted_but_never_shortcut_reused(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-fl-cache-{uuid.uuid4().hex[:8]}"
        )
        run_id_a = f"r-jh-fl-cache-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-fl-cache-b-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(verification_id)
        service.domjudge_register_host("judgehost-fl-cache")

        before_count = self._judge_index_entry_count(RuntimeCacheIndex.RESULT)

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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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
        failed_tests = (
            failed_summary.get("tests") if isinstance(failed_summary, dict) else []
        )
        self.assertIsInstance(failed_tests, list)
        self.assertEqual(str((failed_tests[0] or {}).get("verdict") or ""), "FL")
        self.assertIn("001.in", str(failed_summary.get("error") or ""))
        self.assertIn("judge failed", str(failed_summary.get("error") or "").lower())

        after_fl_count = self._judge_index_entry_count(RuntimeCacheIndex.RESULT)
        self.assertGreaterEqual(after_fl_count, before_count)

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
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        tasks_b = service.domjudge_fetch_work("judgehost-fl-cache", max_batchsize=8)
        self.assertEqual(len(tasks_b), 1)
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_a, case_id_b)

    def test_domjudge_bypass_case_result_cache_bypasses_case_cache(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-recompile-{uuid.uuid4().hex[:8]}"
        )
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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

        original_delete = CaseResultCache.delete
        with patch.object(
            CaseResultCache,
            "delete",
            autospec=True,
            side_effect=original_delete,
        ) as cache_delete:
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
                verification_program_id=_SOLUTION_PROGRAM_ID,
                expected_behavior="accepted",
                verification_source="run.execute",
                bypass_case_result_cache=True,
            )
            tasks_b = service.domjudge_fetch_work(
                "judgehost-recompile", max_batchsize=8
            )
        self.assertEqual(len(tasks_b), 1)
        self.assertGreaterEqual(cache_delete.call_count, 1)
        deleted_refs = {call.args[1:] for call in cache_delete.call_args_list}
        self.assertEqual(len(deleted_refs), 1)
        key_hash, signature = next(iter(deleted_refs))
        self.assertTrue(key_hash)
        self.assertTrue(signature)
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        self.assertGreater(case_id_b, 0)
        self.assertNotEqual(case_id_a, case_id_b)

    def test_domjudge_compiled_job_pending_cases_can_be_shared_across_hosts(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-share-hosts-{uuid.uuid4().hex[:8]}"
        )
        run_id = f"r-jh-share-hosts-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
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
            verification_program_id=_SOLUTION_PROGRAM_ID,
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

    def test_domjudge_fetch_work_defers_preemption_until_inflight_case_reports(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        self._reset_task_queue_state(service)
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-preempt-{uuid.uuid4().hex[:8]}"
        )
        self._seed_build_verification(
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
            selected_tests=["001.in"],
            verification_id=_canonical_verification_id("inv-jh-preempt-low"),
            verification_program_id=_SOLUTION_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            task_kind="solution-run",
        )
        host = "judgehost-priority-preempt"
        service.domjudge_register_host(host)
        first_tasks = service.domjudge_fetch_work(host, max_batchsize=1)
        self.assertEqual(len(first_tasks), 1)
        self.assertEqual(
            len(self._work_rows_for_task(service, first_tasks, low_task_id)), 1
        )

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
            verification_program_id=_GENERATOR_PROGRAM_ID,
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
        self.assertEqual(
            len(self._work_rows_for_task(service, second_tasks, high_task_id)), 1
        )

    def test_domjudge_add_debug_info_preserves_result_and_appends_diagnostic(
        self,
    ) -> None:
        service = runtime.judgehost_task_service
        self._reset_task_queue_state(service)
        verification_id = _canonical_verification_id(str(uuid.uuid4()))
        build_verification_id = _canonical_verification_id(
            f"late-debug-build-{uuid.uuid4()}"
        )
        run_id = f"r-jh-late-debug-{uuid.uuid4().hex[:8]}"
        self._seed_build_verification(
            build_verification_id,
            [("016.in", "ok\n", "ok\n")],
        )
        ctx = runtime.workspace_service.workspace_context(
            self.problem,
            self.user,
            include_recent=False,
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
        )
        self.assertEqual(admission.outcome, "admitted")
        case_task_id = verification_task_id(
            verification_id,
            _ACCEPTED_PROGRAM_ID,
            "016.in",
        )
        tasks = [
            PlannedTask(
                task_id=case_task_id,
                predecessor_task_id=None,
                task_kind="main-correct",
                source_path="solutions/ac.cpp",
                program_id=_ACCEPTED_PROGRAM_ID,
                test_name="016.in",
                expected_behavior="accepted",
            )
        ]
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
        )
        self.assertEqual(activation.outcome, "activated")

        task_store = runtime.verification_task_store
        task_id = service.enqueue_task(
            problem=self.problem,
            username=self.user,
            artifact_verification_id=build_verification_id,
            mode="pass-fail",
            submission_path="solutions/ac.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["016.in"],
            verification_id=verification_id,
            verification_task_id=case_task_id,
            verification_program_id=_ACCEPTED_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            task_kind="main-correct",
        )
        task_snapshot = service.task_snapshot_for_run(run_id)
        self.assertIsNotNone(task_snapshot)
        assert task_snapshot is not None
        self.assertEqual(task_snapshot["id"], task_id)
        batch_rows = service.run_case_snapshots(run_id)
        self.assertEqual(len(batch_rows), 1)
        batch_id = int(batch_rows[0]["batch_id"])
        case_id = self._lease_only_case(service, batch_id, "judgehost-late-debug")
        service.domjudge_update_judging(
            "judgehost-late-debug",
            case_id,
            {"compile_success": "1", "output_compile": "", "compile_metadata": ""},
        )
        self._commit_case_result(
            service,
            case_id=case_id,
            hostname="judgehost-late-debug",
            test_name="016.in",
            runresult="checker-fail",
            verdict="FL",
        )

        before = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["task_kind"]) == "main-correct"
            and str(row["test_name"]) == "016.in"
        )
        self.assertEqual(
            str(before["error_text"] or ""),
            "main solution failed without Judgehost diagnostics for 016.in",
        )
        canonical_result = before["result"]
        verification_row_before = db_fetch_one(
            "SELECT fail_reason FROM verifications WHERE id=?",
            [verification_id],
        )
        self.assertIsNotNone(verification_row_before)

        feedback_text = "Unexpected character #10, but ' ' expected (testdata.in)"
        service.domjudge_add_debug_info(
            hostname="judgehost-late-debug",
            judgetask_id=case_id,
            payload={"message": feedback_text},
        )

        after = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["task_kind"]) == "main-correct"
            and str(row["test_name"]) == "016.in"
        )
        self.assertEqual(after["result"], canonical_result)
        diagnostic_snapshot = task_store.diagnostic_snapshot(case_task_id)
        display = compose_task_diagnostic_display(
            after["result"],
            diagnostic_snapshot,
            limit_bytes=int(runtime.config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
        )
        self.assertEqual(
            [item["text"] for item in display["late_diagnostics"]],
            [feedback_text],
        )
        verification_row = db_fetch_one(
            "SELECT fail_reason FROM verifications WHERE id=?",
            [verification_id],
        )
        self.assertIsNotNone(verification_row)
        assert verification_row is not None
        assert verification_row_before is not None
        self.assertEqual(
            str(verification_row["fail_reason"] or ""),
            str(verification_row_before["fail_reason"] or ""),
        )

    def test_domjudge_groups_distinct_generate_input_tasks_in_one_job(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-shared-generate-{uuid.uuid4().hex[:8]}"
        )
        self._seed_build_verification(verification_id)
        generator_source = (
            b'#include <iostream>\nint main(){ std::cout << "ok\\n"; return 0; }\n'
        )
        validator_source = (
            '#include "testlib.h"\n'
            "int main(){\n"
            "  registerValidation();\n"
            "  inf.readToken();\n"
            "  inf.readEof();\n"
            "  return 0;\n"
            "}\n"
        ).encode("utf-8")
        generator_file = runtime.runtime_blob_store.put_bytes(generator_source)
        validator_file = runtime.runtime_blob_store.put_bytes(validator_source)
        extra_testlib = runtime.runtime_blob_store.put_bytes(b"")
        empty_file = extra_testlib
        payload_base = {
            "run_config_json": json.dumps(
                {
                    "checker_mode": "testlib",
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
            "source_files": {
                "validator.cpp": validator_file.to_payload(),
                "testlib.h": extra_testlib.to_payload(),
            },
        }
        plan_a = VerificationTestPlan(
            test_name="001.in",
            source_kind="gen",
            display_source_path="generators/gen.cpp",
            execution_source_name="gen.cpp",
            execution_source_file=generator_file,
            execution_input_file=runtime.runtime_blob_store.put_bytes(
                b'"$SUBMISSION_BIN" 1\n'
            ),
            extra_source_files={"testlib.h": extra_testlib},
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
            execution_source_file=generator_file,
            execution_input_file=runtime.runtime_blob_store.put_bytes(
                b'"$SUBMISSION_BIN" 2\n'
            ),
            extra_source_files={"testlib.h": extra_testlib},
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

        prepared_a = prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_a,
            test_name="001.in",
            input_file=plan_a.execution_input_file,
            answer_file=empty_file,
            verification_payload_base=payload_base,
            extra_source_files=plan_a.extra_source_files,
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
            verification_program_id=_GENERATOR_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            persist_verification_run=False,
            prepared_payload=prepared_a,
        )
        self.assertTrue(task_id.startswith("jt-"))
        service.domjudge_register_host("judgehost-shared-generate")
        tasks_a = service.domjudge_fetch_work(
            "judgehost-shared-generate", max_batchsize=8
        )
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

        prepared_b = prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_b,
            test_name="002.in",
            input_file=plan_b.execution_input_file,
            answer_file=empty_file,
            verification_payload_base=payload_base,
            extra_source_files=plan_b.extra_source_files,
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
            verification_program_id=_GENERATOR_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            persist_verification_run=False,
            prepared_payload=prepared_b,
        )
        self.assertNotEqual(second_task_id, task_id)

        tasks_b = service.domjudge_fetch_work(
            "judgehost-shared-generate", max_batchsize=8
        )
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

        case_rows = sorted(
            [
                *service.run_case_snapshots(run_id_a),
                *service.run_case_snapshots(run_id_b),
            ],
            key=lambda row: int(row["ordinal"]),
        )
        self.assertTrue(
            all(int(row["batch_id"]) == internal_batch_id for row in case_rows)
        )
        self.assertEqual(
            [str(row["test_name"] or "") for row in case_rows], ["001.in", "002.in"]
        )
        self.assertEqual(
            [str(row["task_id"] or "") for row in case_rows], [task_id, second_task_id]
        )
        self.assertEqual(
            [str(row["run_id"] or "") for row in case_rows],
            [run_id_a, run_id_b],
        )

    def test_domjudge_grouped_batch_uses_stable_uuid_across_fetches(self) -> None:
        service = runtime.judgehost_task_service
        override_config_values(
            self,
            runtime.config_values,
            JUDGEHOST_ENABLE=True,
            JUDGEHOST_API_TOKEN="test-token",
            JUDGEHOST_API_USERNAME="judgehost",
        )

        verification_id = canonical_test_verification_id(
            f"b-jh-grouped-batch-one-{uuid.uuid4().hex[:8]}"
        )
        self._seed_build_verification(verification_id)
        generator_source = (
            b'#include <iostream>\nint main(){ std::cout << "ok\\n"; return 0; }\n'
        )
        validator_source = (
            '#include "testlib.h"\n'
            "int main(){\n"
            "  registerValidation();\n"
            "  inf.readToken();\n"
            "  inf.readEof();\n"
            "  return 0;\n"
            "}\n"
        ).encode("utf-8")
        validator_file = runtime.runtime_blob_store.put_bytes(validator_source)
        extra_testlib = runtime.runtime_blob_store.put_bytes(b"")
        payload_base = {
            "run_config_json": json.dumps(
                {
                    "checker_mode": "testlib",
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
            "source_files": {
                "validator.cpp": validator_file.to_payload(),
                "testlib.h": extra_testlib.to_payload(),
            },
        }
        run_id_a = f"r-jh-grouped-batch-one-a-{uuid.uuid4().hex[:8]}"
        run_id_b = f"r-jh-grouped-batch-one-b-{uuid.uuid4().hex[:8]}"
        prepared_a = prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_a,
            test_name="003.in",
            input_file=runtime.runtime_blob_store.put_bytes(b'"$SUBMISSION_BIN" 3\n'),
            answer_file=extra_testlib,
            verification_payload_base=payload_base,
            extra_source_files={"testlib.h": extra_testlib},
        )
        prepared_b = prepared_payload_for_uploaded_source(
            source_label="gen.cpp",
            run_id=run_id_b,
            test_name="004.in",
            input_file=runtime.runtime_blob_store.put_bytes(b'"$SUBMISSION_BIN" 4\n'),
            answer_file=extra_testlib,
            verification_payload_base=payload_base,
            extra_source_files={"testlib.h": extra_testlib},
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
            verification_program_id=_GENERATOR_PROGRAM_ID,
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
            verification_program_id=_GENERATOR_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="generate-input",
            task_kind="generate-input",
            persist_verification_run=False,
            prepared_payload=prepared_b,
        )
        self.assertNotEqual(task_id_a, task_id_b)

        service.domjudge_register_host("judgehost-grouped-batch-one")
        tasks_a = service.domjudge_fetch_work(
            "judgehost-grouped-batch-one", max_batchsize=1
        )
        self.assertEqual(len(tasks_a), 1)
        self.assertRegex(str(tasks_a[0].get("uuid") or ""), r"^[0-9a-f]{64}$")
        case_id_a = int(tasks_a[0].get("judgetaskid") or 0)
        testcase_id_a = int(tasks_a[0].get("testcase_id") or 0)
        self.assertGreater(testcase_id_a, 0)
        case_row_a = judgehost_fetch_case(service, case_id_a)
        self.assertIsNotNone(case_row_a)
        self.assertEqual(int(case_row_a["testcase_id"] or 0), testcase_id_a)
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

        tasks_b = service.domjudge_fetch_work(
            "judgehost-grouped-batch-one", max_batchsize=1
        )
        self.assertEqual(len(tasks_b), 1)
        self.assertEqual(int(tasks_b[0].get("jobid") or 0), batch_id)
        self.assertEqual(tasks_b[0]["uuid"], tasks_a[0]["uuid"])
        case_id_b = int(tasks_b[0].get("judgetaskid") or 0)
        testcase_id_b = int(tasks_b[0].get("testcase_id") or 0)
        self.assertGreater(testcase_id_b, 0)
        case_row_b = judgehost_fetch_case(service, case_id_b)
        self.assertIsNotNone(case_row_b)
        self.assertEqual(int(case_row_b["testcase_id"] or 0), testcase_id_b)
        self.assertNotEqual(testcase_id_a, testcase_id_b)
        self.assertNotEqual(
            case_id_b,
            case_id_a,
        )
