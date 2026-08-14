import json
import re
from typing import TypedDict

from app.db import now_iso
from app.main_constant import RUN_TEST_NAME_RE
from app.service.judgehost.batch.model import CompileSubmission, ExecutionBatchSpec
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.domjudge.cache import hash_of_hashes
from app.service.judgehost.domjudge.codec import decode_basename, decode_text
from app.service.judgehost.domjudge.identity import submit_id
from app.service.judgehost.domjudge.result import parse_bool
from app.service.judgehost.domjudge.task_plan import task_kind
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.retention import compact_task_row_payload
from app.service.platform.runtime_blob_store import PayloadFile


class PreparedTest(TypedDict):
    name: str
    answer_name: str
    input_file: object
    answer_file: object


class PrecomputedBundle(TypedDict):
    compile_key: str
    source_hash: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    compile_config: dict[str, object]
    run_config: dict[str, object]
    compare_config: dict[str, object]
    compile_files: list[tuple[str, bytes, bool]]
    run_files: list[tuple[str, bytes, bool]]
    compare_files: list[tuple[str, bytes, bool]]
    main_correct: bool


class PreparedPayload(TypedDict):
    source_name: str
    source_file: PayloadFile
    extra_source_items: list[tuple[str, PayloadFile]]
    tests_rows: list[PreparedTest]
    contest_id: str
    mode: str
    compile_key: str
    source_hash: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    compile_config: dict[str, object]
    run_config: dict[str, object]
    compare_config: dict[str, object]
    compile_files: list[tuple[str, bytes, bool]]
    run_files: list[tuple[str, bytes, bool]]
    compare_files: list[tuple[str, bytes, bool]]
    verification_source: str
    expected_behavior: str
    bypass_case_result_cache: bool


class TaskBatchAdmission:
    """Translate one prepared task into an atomic BatchRuntime topology."""

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
        compact_task_row_payload(row)
        self._tasks.update(task_id, {"payload": row["payload"]})

    def _case_rows(
        self,
        *,
        task_id: str,
        verification_task_id: str,
        run_id: str,
        tests_rows: list[PreparedTest],
        scope_sequence: int,
    ) -> list[dict[str, object]]:
        case_rows: list[dict[str, object]] = []
        for ordinal, entry in enumerate(tests_rows, start=1):
            raw_name = entry["name"]
            test_name = raw_name if RUN_TEST_NAME_RE.fullmatch(raw_name) else f"{ordinal:03}.in"
            input_file = PayloadFile.from_payload(entry["input_file"])
            answer_file = PayloadFile.from_payload(entry["answer_file"])
            if input_file.blob_ref is None or answer_file.blob_ref is None:
                raise RuntimeError(
                    "verification testcase payload must be materialized before admission"
                )
            input_hash = input_file.identity
            answer_hash = answer_file.identity
            testcase_hash = hash_of_hashes([input_hash, answer_hash])
            case_rows.append(
                {
                    "task_id": task_id,
                    "verification_task_id": verification_task_id,
                    "run_id": run_id,
                    "test_name": test_name,
                    "ordinal": ordinal,
                    "scope_sequence": scope_sequence,
                    "testcase_id": int(testcase_hash, 16) % (1 << 63),
                    "testcase_hash": testcase_hash,
                    "testcase_input_hash": input_hash,
                    "testcase_answer_hash": answer_hash,
                    "input_ref": input_file.blob_ref,
                    "answer_ref": answer_file.blob_ref,
                    "status": "staged",
                }
            )
        return case_rows

    @staticmethod
    def _task_payload(task: dict[str, object]) -> tuple[str, str, dict[str, object]]:
        task_id = decode_text(raw=task.get("task_id"))
        if not task_id:
            raise RuntimeError("missing task_id for DOMjudge compatibility")
        payload = TaskBatchAdmission._object_mapping(
            task.get("payload"), label="judgehost task payload"
        )
        return task_id, decode_text(raw=task.get("run_id")), payload

    @staticmethod
    def _config_object(raw_json: object) -> dict[str, object]:
        raw_text = decode_text(raw=raw_json)
        if not raw_text:
            return {}
        return TaskBatchAdmission._object_mapping(
            json.loads(raw_text), label="judgehost config"
        )

    @staticmethod
    def _precomputed_bundle(raw: object) -> PrecomputedBundle:
        if not isinstance(raw, dict):
            raise RuntimeError("domjudge precomputed payload is required")
        bundle = TaskBatchAdmission._object_mapping(raw, label="domjudge precomputed payload")
        hash_fields = {
            "compile_key": 64,
            "source_hash": 64,
            "compile_hash": 32,
            "run_hash": 32,
            "compare_hash": 32,
        }
        hashes: dict[str, str] = {}
        for field, width in hash_fields.items():
            value = decode_text(lower=True, raw=bundle.get(field))
            if re.fullmatch(rf"[0-9a-f]{{{width}}}", value) is None:
                raise RuntimeError("domjudge precomputed payload is required")
            hashes[field] = value
        return {
            "compile_key": hashes["compile_key"],
            "source_hash": hashes["source_hash"],
            "compile_hash": hashes["compile_hash"],
            "run_hash": hashes["run_hash"],
            "compare_hash": hashes["compare_hash"],
            "compile_config": TaskBatchAdmission._object_mapping(
                bundle.get("compile_config"), label="compile_config"
            ),
            "run_config": TaskBatchAdmission._object_mapping(
                bundle.get("run_config"), label="run_config"
            ),
            "compare_config": TaskBatchAdmission._object_mapping(
                bundle.get("compare_config"), label="compare_config"
            ),
            "compile_files": TaskBatchAdmission._file_list(
                bundle.get("compile_files"), label="compile_files"
            ),
            "run_files": TaskBatchAdmission._file_list(
                bundle.get("run_files"), label="run_files"
            ),
            "compare_files": TaskBatchAdmission._file_list(
                bundle.get("compare_files"), label="compare_files"
            ),
            "main_correct": TaskBatchAdmission._boolean(
                bundle.get("main_correct"), label="main_correct"
            ),
        }

    def _prepare_payload(self, payload: dict[str, object]) -> PreparedPayload:
        source_name = decode_basename(raw=payload.get("source_name"), default="submission.cpp")
        source_file = PayloadFile.from_payload(payload["source_file"])
        if source_file.size <= 0:
            raise RuntimeError("submission source payload is empty")
        raw_extra_value = payload.get("extra_source_files")
        raw_extra = (
            {}
            if raw_extra_value is None
            else self._object_mapping(raw_extra_value, label="extra_source_files")
        )
        extra_sources = [
            (name, source)
            for raw_name, raw_file in sorted(raw_extra.items())
            if (name := decode_basename(raw=raw_name))
            and name != source_name
            and (source := PayloadFile.from_payload(raw_file)).size > 0
        ]
        raw_verification = payload.get("verification_payload")
        if raw_verification is None:
            raise RuntimeError("verification payload is required for DOMjudge compatibility")
        verification = self._object_mapping(raw_verification, label="verification payload")
        raw_tests = verification.get("tests")
        tests: list[PreparedTest] = []
        if raw_tests is not None:
            if not isinstance(raw_tests, list):
                raise RuntimeError("verification tests must be a list")
            for raw_test in raw_tests:
                test = self._object_mapping(raw_test, label="verification test")
                raw_test_name = test.get("name")
                raw_answer_name = test.get("answer_name")
                if not isinstance(raw_test_name, str):
                    raise RuntimeError("verification test name must be a string")
                if not isinstance(raw_answer_name, str):
                    raise RuntimeError("verification test names must be strings")
                tests.append(
                    {
                        "name": raw_test_name,
                        "answer_name": raw_answer_name,
                        "input_file": test.get("input_file"),
                        "answer_file": test.get("answer_file"),
                    }
                )
        if not tests:
            raise RuntimeError("no tests in judgehost payload")
        precomputed = self._precomputed_bundle(payload.get("precomputed"))
        return {
            "source_name": source_name,
            "source_file": source_file,
            "extra_source_items": extra_sources,
            "tests_rows": tests,
            "contest_id": "local",
            "mode": decode_text(lower=True, raw=payload.get("mode"), default="pass-fail"),
            "compile_key": precomputed["compile_key"],
            "source_hash": precomputed["source_hash"],
            "compile_hash": precomputed["compile_hash"],
            "run_hash": precomputed["run_hash"],
            "compare_hash": precomputed["compare_hash"],
            "compile_config": dict(precomputed["compile_config"]),
            "run_config": dict(precomputed["run_config"]),
            "compare_config": dict(precomputed["compare_config"]),
            "compile_files": list(precomputed["compile_files"]),
            "run_files": list(precomputed["run_files"]),
            "compare_files": list(precomputed["compare_files"]),
            "verification_source": decode_text(
                lower=True, raw=payload.get("verification_source")
            ) or task_kind(payload),
            "expected_behavior": decode_text(
                lower=True, raw=payload.get("expected_behavior")
            ),
            "bypass_case_result_cache": parse_bool(
                payload.get("bypass_case_result_cache"), default=False
            ),
        }

    def stage(self, task: dict[str, object]) -> int:
        task_id, run_id, payload = self._task_payload(task)
        latest = self._tasks.get(task_id)
        if latest is not None:
            payload = latest["payload"].copy()
            run_id = latest["run_id"] or run_id
        prepared = self._prepare_payload(payload)
        verification_id = decode_text(raw=payload.get("verification_id"))
        if not verification_id:
            raise RuntimeError("execution scope id is required")
        program_id = decode_text(raw=payload.get("verification_program_id"))
        service_class = decode_text(
            lower=True, raw=payload.get("service_class"), default="background"
        )
        if service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        compile_submission = CompileSubmission(
            compile_key=prepared["compile_key"],
            submit_id=submit_id(prepared["compile_key"]),
            source_name=prepared["source_name"],
            source_file=prepared["source_file"],
            extra_source_items=tuple(prepared["extra_source_items"]),
            compile_files=tuple(prepared["compile_files"]),
        )
        return self._batch_runtime.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            verification_program_id=program_id,
            execution_signature=decode_text(raw=payload.get("execution_signature")),
            task_kind=task_kind(payload),
            verification_id=verification_id,
            compile_key=prepared["compile_key"],
            compile_submission=compile_submission,
            contest_id=prepared["contest_id"],
            mode=prepared["mode"],
            source_name=prepared["source_name"],
            compile_hash=prepared["compile_hash"],
            run_hash=prepared["run_hash"],
            compare_hash=prepared["compare_hash"],
            source_hash=prepared["source_hash"],
            compile_config_json=json.dumps(
                prepared["compile_config"], ensure_ascii=False, separators=(",", ":")
            ),
            run_config_json=json.dumps(
                prepared["run_config"], ensure_ascii=False, separators=(",", ":")
            ),
            compare_config_json=json.dumps(
                prepared["compare_config"], ensure_ascii=False, separators=(",", ":")
            ),
            expected_behavior=prepared["expected_behavior"],
            verification_source=prepared["verification_source"],
            bypass_case_result_cache=1 if prepared["bypass_case_result_cache"] else 0,
            service_class=service_class,
            batch_spec=ExecutionBatchSpec(
                run_files=tuple(prepared["run_files"]),
                compare_files=tuple(prepared["compare_files"]),
            ),
            created_at=now_iso(),
            case_rows=self._case_rows(
                task_id=task_id,
                verification_task_id=decode_text(raw=payload.get("verification_task_id")),
                run_id=run_id,
                tests_rows=prepared["tests_rows"],
                scope_sequence=self._batch_runtime.scope_sequence(verification_id),
            ),
        )

    def activate(self, task_id: str) -> bool:
        return self._batch_runtime.activate_task_cases(task_id, now_text=now_iso())

    @staticmethod
    def _object_mapping(value: object, *, label: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} must be an object")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeError(f"{label} keys must be strings")
            result[key] = item
        return result

    @staticmethod
    def _boolean(value: object, *, label: str) -> bool:
        if not isinstance(value, bool):
            raise RuntimeError(f"{label} must be a boolean")
        return value

    @staticmethod
    def _file_list(value: object, *, label: str) -> list[tuple[str, bytes, bool]]:
        if not isinstance(value, list):
            raise RuntimeError(f"{label} must be a list")
        result: list[tuple[str, bytes, bool]] = []
        for item in value:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 3
                or not isinstance(item[0], str)
                or not isinstance(item[1], bytes)
                or not isinstance(item[2], bool)
            ):
                raise RuntimeError(f"{label} contains an invalid file entry")
            result.append((item[0], item[1], item[2]))
        return result
