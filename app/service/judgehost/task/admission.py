import uuid

from app.db import now_iso
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.domjudge import task_plan
from app.service.judgehost.ports.case_binding import CaseBinding, CaseBindingPort
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate
from app.service.judgehost.task.batch_admission import TaskBatchAdmission
from app.service.judgehost.task.preparation import JudgehostPayloadPreparation
from app.service.judgehost.task.registry import JudgehostTaskRegistry, JudgehostTaskRow
from app.service.judgehost.validation import normalize_run_id
from app.service.judgehost.validation import normalize_verification_program_id
from app.service.platform.runtime_blob_store import PayloadFile


class JudgehostTaskAdmission:
    """Own task identity, batch staging, durable binding, and exposure."""

    def __init__(
        self,
        preparation: JudgehostPayloadPreparation,
        execution_port: CaseBindingPort,
        batch_runtime: JudgehostBatchRuntime,
        tasks: JudgehostTaskRegistry,
        batch_admission: TaskBatchAdmission,
    ) -> None:
        self._preparation = preparation
        self._execution_port = execution_port
        self._batch_runtime = batch_runtime
        self._tasks = tasks
        self._batch_admission = batch_admission

    @staticmethod
    def _initial_summary(
        *,
        task_id: str,
        mode: str,
        pass_limit: int,
        source_label: str,
        selected_tests: list[str],
        verification_source: str,
        task_kind: str,
    ) -> dict[str, object]:
        summary: dict[str, object] = {
            "mode": mode,
            "pass_limit": max(1, pass_limit),
            "source": source_label,
            "selected_tests": list(selected_tests),
            "selected_tests_count": len(selected_tests),
            "verification_source": verification_source,
            "task_kind": task_kind,
            "tests": [],
            "compile_log": "",
            "compile_diagnostics": [],
            "toolchain_digest": "judgehost",
            "limits": {},
            "usage": {},
            "judgehost": {"task_id": task_id, "status": "queued"},
        }
        if task_kind == "compile-only":
            summary["compile_only"] = True
        return summary

    def enqueue_task(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_file: PayloadFile | None = None,
        upload_filename: str | None,
        run_id: str | None = None,
        selected_tests: list[str] | None,
        verification_id: str,
        verification_task_id: str = "",
        verification_program_id: str,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
        persist_verification_run: bool = False,
        prepared_payload: dict[str, object] | None = None,
        execution_template: dict[str, object] | None = None,
        service_class: str = "background",
        admission_gate: MaintenanceAdmissionGate | None = None,
    ) -> str:
        if admission_gate is not None:
            with admission_gate.locked():
                if not admission_gate.is_open_locked():
                    raise RuntimeError(
                        "maintenance in progress: judgehost admission is closed"
                    )
        safe_run_id = normalize_run_id(run_id if run_id else verification_id)
        safe_verification_id = self._preparation.verification_id(verification_id)
        safe_program_id = normalize_verification_program_id(verification_program_id)
        safe_verification_task_id = verification_task_id.strip()
        selected = self._preparation.normalize_tests(selected_tests)
        verification_payload_override: dict[str, object] | None = None
        source_label_override: str | None = None
        extra_source_files_override: dict[str, object] | None = None
        manual_validate_only = False
        if prepared_payload is not None and "verification_payload" in prepared_payload:
            raw_verification_payload = prepared_payload["verification_payload"]
            if not isinstance(raw_verification_payload, dict) or any(
                not isinstance(key, str) for key in raw_verification_payload
            ):
                raise RuntimeError("judgehost verification payload must be an object")
            verification_payload_override = dict(raw_verification_payload)
        if prepared_payload is not None and "source_label" in prepared_payload:
            raw_source_label = prepared_payload["source_label"]
            if not isinstance(raw_source_label, str):
                raise RuntimeError("judgehost source label must be a string")
            source_label_override = raw_source_label
        if prepared_payload is not None and "extra_source_files" in prepared_payload:
            raw_extra_sources = prepared_payload["extra_source_files"]
            if not isinstance(raw_extra_sources, dict) or any(
                not isinstance(key, str) for key in raw_extra_sources
            ):
                raise RuntimeError("judgehost extra source files must be an object")
            extra_source_files_override = dict(raw_extra_sources)
        if prepared_payload is not None and "manual_validate_only" in prepared_payload:
            raw_manual_validate_only = prepared_payload["manual_validate_only"]
            if not isinstance(raw_manual_validate_only, bool):
                raise RuntimeError("manual validate flag must be a boolean")
            manual_validate_only = raw_manual_validate_only
        payload = self._preparation.prepare_enqueue_payload(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_file=upload_file,
            upload_filename=upload_filename,
            run_id=safe_run_id,
            selected_tests=selected,
            verification_id=safe_verification_id,
            verification_task_id=safe_verification_task_id,
            verification_program_id=safe_program_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            bypass_case_result_cache=bypass_case_result_cache,
            compile_only=compile_only,
            verification_payload_override=verification_payload_override,
            source_label_override=source_label_override,
            extra_source_files_override=extra_source_files_override,
            manual_validate_only=manual_validate_only,
            execution_template=execution_template,
        )
        payload["execution_signature"] = task_plan.execution_signature(payload)
        safe_task_kind = task_plan.task_kind(payload)
        safe_mode = payload.get("mode")
        if not isinstance(safe_mode, str) or safe_mode not in {
            "pass-fail",
            "interactive",
        }:
            raise RuntimeError("judgehost execution mode is invalid")
        safe_service_class = service_class.strip().lower()
        if safe_service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        payload.update(
            {
                "run_id": safe_run_id,
                "problem": problem,
                "username": username,
                "artifact_verification_id": artifact_verification_id,
                "mode": safe_mode,
                "submission_path": submission_path or "",
                "selected_tests": list(selected),
                "verification_id": safe_verification_id,
                "verification_task_id": safe_verification_task_id,
                "verification_program_id": safe_program_id,
                "expected_behavior": expected_behavior,
                "verification_source": verification_source,
                "task_kind": safe_task_kind,
                "bypass_case_result_cache": bypass_case_result_cache,
                "compile_only": safe_task_kind == "compile-only",
                "service_class": safe_service_class,
            }
        )
        def stage() -> tuple[str, int]:
            task_id, inserted = self._insert_task(
                payload=payload,
                fingerprint=self._preparation.enqueue_fingerprint(payload),
                run_id=safe_run_id,
                problem=problem,
                username=username,
                artifact_verification_id=artifact_verification_id,
                mode=safe_mode,
                verification_id=safe_verification_id,
                verification_task_id=safe_verification_task_id,
                verification_source=verification_source,
                task_kind=safe_task_kind,
                selected_tests=selected,
                persist_verification_run=persist_verification_run,
            )
            if not inserted or self._batch_runtime.batch_for_task(task_id) is not None:
                return (task_id, 0)
            batch_id = self._stage_task(task_id=task_id, run_id=safe_run_id, payload=payload)
            return (task_id, batch_id)

        if admission_gate is None:
            task_id, batch_id = stage()
        else:
            with admission_gate.locked():
                if not admission_gate.is_open_locked():
                    raise RuntimeError(
                        "maintenance in progress: judgehost admission is closed"
                    )
                task_id, batch_id = stage()
        if not batch_id:
            return task_id
        self._expose_staged_task(
            task_id=task_id,
            batch_id=batch_id,
            run_id=safe_run_id,
            verification_id=safe_verification_id,
            verification_task_id=safe_verification_task_id,
            verification_program_id=safe_program_id,
            payload=payload,
        )
        return task_id

    def _insert_task(
        self,
        *,
        payload: dict[str, object],
        fingerprint: str,
        run_id: str,
        problem: str,
        username: str,
        artifact_verification_id: str,
        mode: str,
        verification_id: str,
        verification_task_id: str,
        verification_source: str,
        task_kind: str,
        selected_tests: list[str],
        persist_verification_run: bool,
    ) -> tuple[str, bool]:
        while True:
            existing = self._tasks.get_for_run(run_id)
            if existing is not None:
                if existing["enqueue_fingerprint"] != fingerprint:
                    raise RuntimeError("judgehost run id reused with different payload")
                if existing["status"] != "enqueuing":
                    return (existing["id"], False)
                generation = self._tasks.change_generation()
                self._tasks.wait_for_change(generation, 0.05)
                continue
            task_id = f"jt-{uuid.uuid4().hex[:12]}"
            source = payload.get("source_label") or payload.get("source_name") or "upload"
            now_text = now_iso()
            row: JudgehostTaskRow = {
                "id": task_id,
                "run_id": run_id,
                "problem_slug": problem,
                "username": username,
                "artifact_verification_id": artifact_verification_id,
                "mode": mode,
                "verification_id": verification_id,
                "verification_task_id": verification_task_id,
                "status": "enqueuing",
                "payload": dict(payload),
                "result": {},
                "persist_verification_run": persist_verification_run,
                "error_text": "",
                "created_at": now_text,
                "updated_at": now_text,
                "completed_at": "",
                "summary": self._initial_summary(
                    task_id=task_id,
                    mode=mode,
                    pass_limit=self._preparation.precomputed_pass_limit(payload),
                    source_label=str(source),
                    selected_tests=selected_tests,
                    verification_source=verification_source,
                    task_kind=task_kind,
                ),
                "enqueue_fingerprint": fingerprint,
            }
            try:
                self._tasks.insert(row)
                return (task_id, True)
            except RuntimeError as exc:
                if str(exc) != "judgehost task already exists":
                    raise

    def _stage_task(
        self,
        *,
        task_id: str,
        run_id: str,
        payload: dict[str, object],
    ) -> int:
        batch_id = 0
        try:
            batch_id = self._batch_admission.stage(
                {"task_id": task_id, "run_id": run_id, "payload": dict(payload)}
            )
            queued = self._tasks.transition(
                task_id,
                expected={"enqueuing"},
                status="queued",
                updates={"updated_at": now_iso()},
            )
            if queued is None:
                raise RuntimeError("judgehost task staging lost its queued transition")
            return batch_id
        except Exception as exc:
            if batch_id:
                self._batch_runtime.discard_staged_task_cases(
                    task_id,
                    batch_id=batch_id,
                )
            finished_at = now_iso()
            self._tasks.transition(
                task_id,
                expected={"enqueuing", "queued"},
                status="failed",
                updates={
                    "result": {"run_status": "failed", "error": str(exc)},
                    "error_text": str(exc),
                    "updated_at": finished_at,
                    "completed_at": finished_at,
                },
            )
            raise

    def _expose_staged_task(
        self,
        *,
        task_id: str,
        batch_id: int,
        run_id: str,
        verification_id: str,
        verification_task_id: str,
        verification_program_id: str,
        payload: dict[str, object],
    ) -> None:
        try:
            def expose() -> None:
                if not self._batch_admission.activate(task_id):
                    raise RuntimeError("judgehost task staged no cases")

            if verification_task_id:
                bindings = tuple(
                    CaseBinding(
                        execution_scope_id=verification_id,
                        program_id=verification_program_id,
                        task_id=verification_task_id,
                        test_name=case["test_name"],
                    )
                    for case in self._batch_runtime.cases_for_task(task_id)
                )
                accepted = self._execution_port.bind_and_expose(
                    bindings,
                    run_id=run_id,
                    judgehost_task_id=task_id,
                    expose=expose,
                )
            else:
                expose()
                accepted = True
            if not accepted:
                raise RuntimeError("verification task refused judgehost runtime binding")
            self._batch_admission.complete_exposure(task_id)
        except Exception as exc:
            if batch_id:
                self._batch_runtime.discard_staged_task_cases(task_id, batch_id=batch_id)
            finished_at = now_iso()
            self._tasks.transition(
                task_id,
                expected={"enqueuing", "queued"},
                status="failed",
                updates={
                    "result": {"run_status": "failed", "error": str(exc)},
                    "error_text": str(exc),
                    "updated_at": finished_at,
                    "completed_at": finished_at,
                },
            )
            raise

    def enqueue_compile_only_task(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        upload_content: bytes,
        upload_filename: str,
        run_id: str,
        verification_id: str,
        verification_program_id: str,
        expected_behavior: str = "compile",
        verification_source: str = "compile.only",
        prepared_payload: dict[str, object] | None = None,
        admission_gate: MaintenanceAdmissionGate | None = None,
    ) -> str:
        return self.enqueue_task(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            submission_path=None,
            upload_content=upload_content,
            upload_filename=upload_filename or "submission.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=verification_id,
            verification_program_id=verification_program_id,
            expected_behavior=expected_behavior or "compile",
            verification_source=verification_source or "compile.only",
            task_kind="compile-only",
            compile_only=True,
            persist_verification_run=False,
            prepared_payload=prepared_payload,
            service_class="foreground",
            admission_gate=admission_gate,
        )
