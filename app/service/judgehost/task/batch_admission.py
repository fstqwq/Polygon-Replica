from app.db import now_iso
from app.main_constant import RUN_TEST_NAME_RE
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.batch.model import CaseInput
from app.service.judgehost.domjudge.codec import decode_text
from app.service.judgehost.task.model import ExecutionTemplate, PreparedTest, TaskPayload
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.retention import compact_payload_for_retention


class TaskBatchAdmission:
    """Bind canonical program and testcase preparation to one runtime task."""

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        tasks: JudgehostTaskRegistry,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._tasks = tasks

    def complete_exposure(self, task_id: str) -> None:
        row = self._tasks.get(task_id)
        if row is None:
            return
        row["payload"] = compact_payload_for_retention(row["payload"])
        self._tasks.update(task_id, {"payload": row["payload"]})

    def _case_rows(
        self,
        *,
        task_id: str,
        verification_task_id: str,
        run_id: str,
        tests_rows: list[PreparedTest],
        scope_sequence: int,
    ) -> list[CaseInput]:
        return [
            {
                "task_id": task_id,
                "verification_task_id": verification_task_id,
                "run_id": run_id,
                "test_name": entry.name if RUN_TEST_NAME_RE.fullmatch(entry.name) else f"{ordinal:03}.in",
                "ordinal": ordinal,
                "scope_sequence": scope_sequence,
                "testcase_id": entry.testcase_id,
                "testcase_hash": entry.testcase_hash,
                "testcase_input_hash": entry.input_file.identity,
                "testcase_answer_hash": entry.answer_file.identity,
                "input_ref": entry.input_file.blob_ref,
                "answer_ref": entry.answer_file.blob_ref,
                "status": "staged",
            }
            for ordinal, entry in enumerate(tests_rows, start=1)
        ]

    @staticmethod
    def _prepare_payload(payload: TaskPayload) -> tuple[ExecutionTemplate, list[PreparedTest]]:
        template = payload.get("precomputed")
        if not isinstance(template, ExecutionTemplate):
            raise RuntimeError("prepared execution template is required")
        verification = payload.get("verification_payload")
        if not isinstance(verification, dict):
            raise RuntimeError("verification payload is required")
        raw_tests = verification.get("tests")
        if not isinstance(raw_tests, list) or not raw_tests:
            raise RuntimeError("no tests in judgehost payload")
        tests: list[PreparedTest] = []
        for test in raw_tests:
            if not isinstance(test, PreparedTest):
                raise RuntimeError("prepared testcase is required")
            tests.append(test)
        return template, tests

    def stage(self, *, task_id: str, run_id: str, payload: TaskPayload) -> int:
        task_id = decode_text(raw=task_id)
        if not task_id:
            raise RuntimeError("missing task_id for DOMjudge compatibility")
        latest = self._tasks.get(task_id)
        payload = latest["payload"] if latest is not None else payload
        run_id = decode_text(raw=latest["run_id"] if latest is not None else run_id)
        template, tests = self._prepare_payload(payload)
        verification_id = decode_text(raw=payload.get("verification_id"))
        if not verification_id:
            raise RuntimeError("execution scope id is required")
        program_id = decode_text(raw=payload.get("verification_program_id"))
        service_class = decode_text(lower=True, raw=payload.get("service_class"), default="background")
        if service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        kind, verification_source, expected_behavior, bypass_cache = template.policy
        return self._batch_runtime.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            verification_program_id=program_id,
            execution_signature=template.execution_signature,
            task_kind=kind,
            verification_id=verification_id,
            compile_key=template.submission.compile_key,
            compile_submission=template.submission,
            contest_id="local",
            mode=decode_text(lower=True, raw=payload.get("mode"), default="pass-fail"),
            source_name=template.submission.source_name,
            compile_hash=template.compile_hash,
            run_hash=template.run_hash,
            compare_hash=template.compare_hash,
            source_hash=template.source_hash,
            compile_config_json=template.compile_config_json,
            run_config_json=template.run_config_json,
            compare_config_json=template.compare_config_json,
            expected_behavior=expected_behavior,
            verification_source=verification_source or kind,
            bypass_case_result_cache=int(bypass_cache),
            service_class=service_class,
            batch_spec=template.batch_spec,
            created_at=now_iso(),
            case_rows=self._case_rows(
                task_id=task_id,
                verification_task_id=decode_text(raw=payload.get("verification_task_id")),
                run_id=run_id,
                tests_rows=tests,
                scope_sequence=self._batch_runtime.scope_sequence(verification_id),
            ),
        )

    def activate(self, task_id: str) -> bool:
        return self._batch_runtime.activate_task_cases(task_id, now_text=now_iso())
