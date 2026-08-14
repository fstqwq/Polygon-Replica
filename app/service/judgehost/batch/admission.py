from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING

from app.service.judgehost.batch.model import (
    CaseSpec,
    CompileSubmission,
    ExecutionBatchRecord,
    ExecutionBatchSpec,
    JudgehostCaseRow,
    StatusCounts,
)
from app.service.judgehost.batch.snapshot import case_snapshot
from app.service.judgehost.domjudge.identity import script_id
from app.service.judgehost.domjudge.identity import job_id, submit_id

if TYPE_CHECKING:
    from app.service.judgehost.batch.state import BatchState


class BatchAdmission:
    """Own atomic admission and verification-topology transitions."""

    def __init__(self, state: "BatchState") -> None:
        self._state = state

    def scope_sequence(self, verification_id: str) -> int:
        token = verification_id or "__direct__"
        with self._state._lock:
            sequence = self._state._scope_sequence_by_verification.get(token)
            if sequence is None:
                sequence = next(self._state._sequence)
                self._state._scope_sequence_by_verification[token] = sequence
            return sequence

    def forget_scope(self, verification_id: str) -> None:
        token = verification_id or "__direct__"
        with self._state._lock:
            self._state._scope_sequence_by_verification.pop(token, None)
            if not self._state._batch_ids_by_verification.get(token):
                self._state._closed_verification_ids.discard(token)
                self._state._closed_program_keys = {
                    key for key in self._state._closed_program_keys if key[0] != token
                }

    def finish_verification_execution(self, verification_id: str, *, now_text: str) -> list[int]:
        """Close one execution scope and expose its terminal Batches for finalization."""
        token = verification_id or "__direct__"
        with self._state._lock:
            self._state._closed_verification_ids.add(token)
            ready: list[int] = []
            for batch_id in self._state._batch_ids_by_verification.get(token, ()):
                batch = self._state._batches[batch_id]
                self._state._closed_program_keys.add((token, batch.verification_program_id))
                counts = self._state._batch_counts[batch_id]
                if (
                    batch.status == "open"
                    and counts.total > 0
                    and counts.terminal == counts.total
                    and batch.materialization_state != "materializing"
                ):
                    self._state._close_batch_locked(batch, updated_at=now_text)
                if batch.status == "finalize-pending":
                    ready.append(batch_id)
            return ready

    def finish_programs(
        self,
        verification_id: str,
        verification_program_ids: Iterable[str],
        *,
        now_text: str,
    ) -> list[int]:
        """Stop program admission and expose terminal Batches for finalization."""
        token = verification_id or "__direct__"
        with self._state._lock:
            ready: list[int] = []
            for program_id in dict.fromkeys(verification_program_ids):
                key = (token, program_id)
                self._state._closed_program_keys.add(key)
                batch_id = self._state._batch_id_by_verification_program.get(key)
                if batch_id is None:
                    continue
                batch = self._state._batches[batch_id]
                counts = self._state._batch_counts[batch_id]
                if (
                    batch.status == "open"
                    and counts.total > 0
                    and counts.terminal == counts.total
                    and batch.materialization_state != "materializing"
                ):
                    self._state._close_batch_locked(batch, updated_at=now_text)
                if batch.status == "finalize-pending":
                    ready.append(batch_id)
            return ready

    def create_batch_with_cases(
        self,
        *,
        task_id: str,
        run_id: str,
        verification_program_id: str,
        execution_signature: str,
        task_kind: str,
        verification_id: str,
        compile_key: str,
        compile_submission: CompileSubmission,
        contest_id: str,
        mode: str,
        source_name: str,
        compile_hash: str,
        run_hash: str,
        compare_hash: str,
        source_hash: str,
        compile_config_json: str,
        run_config_json: str,
        compare_config_json: str,
        expected_behavior: str,
        verification_source: str,
        bypass_case_result_cache: int,
        service_class: str,
        batch_spec: ExecutionBatchSpec,
        created_at: str,
        case_rows: list[dict[str, object]],
    ) -> int:
        cases = self._normalize_case_rows(case_rows, default_task_id=task_id, default_run_id=run_id)
        if service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        if not verification_program_id:
            raise RuntimeError("missing judgehost verification program id")
        if task_kind not in {
            "compile-only",
            "generate-input",
            "main-correct",
            "solution-run",
        }:
            raise RuntimeError("invalid judgehost task kind")
        for script_hash in (compile_hash, run_hash, compare_hash):
            if script_hash:
                script_id(script_hash)
        if compile_submission.compile_key != compile_key:
            raise RuntimeError("compile submission key mismatch")
        if compile_submission.submit_id != submit_id(compile_key):
            raise RuntimeError("compile submission id mismatch")
        if len(execution_signature) != 64 or any(
            char not in "0123456789abcdef" for char in execution_signature
        ):
            raise RuntimeError("invalid judgehost execution signature")
        with self._state._lock:
            self._validate_testcase_identities_locked(cases)
            if verification_id in self._state._closed_verification_ids:
                raise RuntimeError("judgehost verification execution is closed")
            program_key = (verification_id, verification_program_id)
            if program_key in self._state._closed_program_keys:
                raise RuntimeError("judgehost verification program is closed")
            existing_batch_id = self._state._batch_id_by_verification_program.get(program_key)
            if existing_batch_id is not None:
                existing_batch = self._state._batches[existing_batch_id]
                identity = (
                    existing_batch.execution_signature,
                    existing_batch.task_kind,
                    existing_batch.compile_key,
                    existing_batch.contest_id,
                    existing_batch.mode,
                    existing_batch.source_name,
                    existing_batch.compile_hash,
                    existing_batch.run_hash,
                    existing_batch.compare_hash,
                    existing_batch.source_hash,
                    existing_batch.compile_config_json,
                    existing_batch.run_config_json,
                    existing_batch.compare_config_json,
                    existing_batch.expected_behavior,
                    existing_batch.verification_source,
                    existing_batch.bypass_case_result_cache,
                    existing_batch.service_class,
                    self._state._batch_specs[existing_batch_id],
                    self._state._compile_submission_identity(
                        self._state._compile_submissions_by_key[existing_batch.compile_key]
                    ),
                )
                requested_identity = (
                    execution_signature,
                    task_kind,
                    compile_key,
                    contest_id,
                    mode,
                    source_name,
                    compile_hash,
                    run_hash,
                    compare_hash,
                    source_hash,
                    compile_config_json,
                    run_config_json,
                    compare_config_json,
                    expected_behavior,
                    verification_source,
                    int(bypass_case_result_cache),
                    service_class,
                    batch_spec,
                    self._state._compile_submission_identity(compile_submission),
                )
                if identity != requested_identity:
                    raise RuntimeError("judgehost verification program identity changed")
                if existing_batch.status != "open":
                    raise RuntimeError("judgehost verification program is closed")
                self._append_cases_to_batch_locked(
                    batch=existing_batch,
                    cases=cases,
                    now_text=created_at,
                )
                return existing_batch_id
            protocol_job_id = job_id(verification_id)
            existing_verification_id = self._state._verification_by_job_id.get(protocol_job_id)
            if existing_verification_id not in {None, verification_id}:
                raise RuntimeError("DOMjudge batch id collision")
            existing_compile_key = self._state._compile_key_by_submit_id.get(
                compile_submission.submit_id
            )
            if existing_compile_key not in {None, compile_key}:
                raise RuntimeError("DOMjudge submit id collision")
            existing_submission = self._state._compile_submissions_by_key.get(compile_key)
            if existing_submission is not None and self._state._compile_submission_identity(
                existing_submission
            ) != self._state._compile_submission_identity(compile_submission):
                raise RuntimeError("compile submission identity changed")
            case_task_ids = {case.task_id for case in cases}
            if any(case_task_id in self._state._batch_id_by_task for case_task_id in case_task_ids):
                raise RuntimeError("judgehost task cases already belong to another batch")
            if task_id in self._state._batch_id_by_task or run_id in self._state._batch_ids_by_run:
                raise RuntimeError("judgehost batch identity already exists")
            entity_ids = self._state._next_entity_ids_locked(len(cases) + 1)
            batch_id = entity_ids[0]
            batch = ExecutionBatchRecord(
                batch_id=batch_id,
                verification_program_id=verification_program_id,
                execution_signature=str(execution_signature),
                task_kind=task_kind,
                verification_id=verification_id,
                job_id=protocol_job_id,
                compile_key=compile_key,
                contest_id=contest_id,
                mode=mode,
                source_name=source_name,
                compile_hash=compile_hash,
                run_hash=run_hash,
                compare_hash=compare_hash,
                source_hash=source_hash,
                compile_config_json=compile_config_json,
                run_config_json=run_config_json,
                compare_config_json=compare_config_json,
                expected_behavior=expected_behavior,
                verification_source=verification_source,
                bypass_case_result_cache=int(bypass_case_result_cache),
                compile_success=None,
                compile_state="unknown",
                materialization_state="unmaterialized",
                service_class=service_class,
                dispatch_count=0,
                compile_output_b64=None,
                compile_metadata_b64=None,
                debug_text="",
                failure_runresult="",
                failure_text="",
                program_failure_result=None,
                program_failure_diagnostic_digest="",
                status="open",
                created_at=created_at,
                updated_at=created_at,
                completed_at=None,
            )
            self._state._batches[batch_id] = batch
            self._state._batch_specs[batch_id] = batch_spec
            if existing_submission is None:
                self._state._compile_submissions_by_key[compile_key] = compile_submission
            self._state._compile_key_by_submit_id[compile_submission.submit_id] = compile_key
            self._state._batch_ids_by_compile_key[compile_key].add(batch_id)
            self._state._batch_ids_by_verification[verification_id].add(batch_id)
            self._state._verification_by_job_id[protocol_job_id] = verification_id
            self._state._batch_counts[batch_id] = StatusCounts()
            self._state._batch_id_by_verification_program[program_key] = batch_id
            self._state._index_batch_scripts_locked(batch, 1)
            self._state._empty_batch_ids.add(batch_id)
            for case_id, case in zip(entity_ids[1:], cases, strict=True):
                self._state._insert_case_locked(
                    case_id=case_id,
                    batch_id=batch_id,
                    source=case,
                    created_at=created_at,
                )
            return batch_id

    @staticmethod
    def _integer(value: object, *, field: str) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError as exc:
                raise RuntimeError(f"invalid judgehost case {field}") from exc
        raise RuntimeError(f"invalid judgehost case {field}")

    @staticmethod
    def _string(value: object, *, field: str) -> str:
        if not isinstance(value, str):
            raise RuntimeError(f"invalid judgehost case {field}")
        return value

    @classmethod
    def _normalize_case_rows(
        cls,
        case_rows: list[dict[str, object]],
        *,
        default_task_id: str = "",
        default_run_id: str = "",
    ) -> list[CaseSpec]:
        required_fields = {
            "test_name",
            "ordinal",
            "testcase_id",
            "testcase_hash",
            "testcase_input_hash",
            "testcase_answer_hash",
            "input_ref",
            "answer_ref",
        }
        identities: set[tuple[str, str]] = set()
        cases: list[CaseSpec] = []
        for row in case_rows:
            missing = required_fields.difference(row)
            if missing:
                raise RuntimeError(f"invalid judgehost case spec: missing {sorted(missing)[0]}")
            task_id = cls._string(
                row.get("task_id") or default_task_id,
                field="task_id",
            )
            run_id = cls._string(
                row.get("run_id") or default_run_id,
                field="run_id",
            )
            test_name = cls._string(row["test_name"], field="test_name")
            if not task_id or not run_id or not test_name:
                raise RuntimeError("invalid judgehost case identity")
            identity = (task_id, test_name)
            if identity in identities:
                raise RuntimeError("duplicate judgehost task case")
            identities.add(identity)
            ordinal = cls._integer(row["ordinal"], field="ordinal")
            scope_sequence = cls._integer(
                row.get("scope_sequence") or 1,
                field="scope_sequence",
            )
            raw_testcase_id = row["testcase_id"]
            testcase_id = (
                None
                if raw_testcase_id is None
                else cls._integer(raw_testcase_id, field="testcase_id")
            )
            status = cls._string(row.get("status") or "staged", field="status")
            if status not in {"staged", "cache-pending", "pending"}:
                raise RuntimeError("invalid judgehost case status")
            cases.append(
                CaseSpec(
                    verification_task_id=cls._string(
                        row.get("verification_task_id") or "",
                        field="verification_task_id",
                    ),
                    task_id=task_id,
                    run_id=run_id,
                    test_name=test_name,
                    ordinal=ordinal,
                    scope_sequence=scope_sequence,
                    testcase_id=testcase_id,
                    testcase_hash=cls._string(row["testcase_hash"], field="testcase_hash"),
                    testcase_input_hash=cls._string(
                        row["testcase_input_hash"], field="testcase_input_hash"
                    ),
                    testcase_answer_hash=cls._string(
                        row["testcase_answer_hash"], field="testcase_answer_hash"
                    ),
                    input_ref=cls._string(row["input_ref"], field="input_ref"),
                    answer_ref=cls._string(row["answer_ref"], field="answer_ref"),
                    status=status,
                )
            )
        return cases

    @staticmethod
    def _case_identity(case: CaseSpec) -> tuple[object, ...]:
        return (
            case.verification_task_id,
            case.run_id,
            case.test_name,
            case.ordinal,
            case.scope_sequence,
            case.testcase_id,
            case.testcase_hash,
            case.testcase_input_hash,
            case.testcase_answer_hash,
            case.input_ref,
            case.answer_ref,
        )

    @staticmethod
    def _stored_case_identity(row: JudgehostCaseRow) -> tuple[object, ...]:
        return (
            row["verification_task_id"],
            row["run_id"],
            row["test_name"],
            row["ordinal"],
            row["scope_sequence"],
            row["testcase_id"],
            row["testcase_hash"],
            row["testcase_input_hash"],
            row["testcase_answer_hash"],
            row["input_ref"],
            row["answer_ref"],
        )

    def _append_cases_to_batch_locked(
        self,
        *,
        batch: ExecutionBatchRecord,
        cases: list[CaseSpec],
        now_text: str,
    ) -> None:
        if batch.status != "open" or batch.verification_id in self._state._closed_verification_ids:
            raise RuntimeError("judgehost verification execution is closed")
        self._validate_testcase_identities_locked(cases)
        rows_by_task: dict[str, list[CaseSpec]] = defaultdict(list)
        new_cases: list[CaseSpec] = []
        for case in cases:
            rows_by_task[case.task_id].append(case)
        for case_task_id, requested_rows in rows_by_task.items():
            existing_batch_id = self._state._batch_id_by_task.get(case_task_id)
            if existing_batch_id is not None and existing_batch_id != batch.batch_id:
                raise RuntimeError("judgehost task cases already belong to another batch")
            if existing_batch_id is None:
                continue
            existing_rows = [
                case_snapshot(self._state._cases[case_id])
                for case_id in self._state._case_ids_by_task[case_task_id]
            ]
            requested = sorted((self._case_identity(case) for case in requested_rows), key=repr)
            existing = sorted((self._stored_case_identity(row) for row in existing_rows), key=repr)
            if requested != existing:
                raise RuntimeError("judgehost task case set is immutable")
        for case in cases:
            pair = (case.task_id, case.test_name)
            existing_case_id = self._state._latest_case_id_by_task_test.get(pair)
            if existing_case_id is not None:
                if self._state._cases[existing_case_id].batch_id != batch.batch_id:
                    raise RuntimeError("judgehost task cases already belong to another batch")
                continue
            new_cases.append(case)
        for case_id, case in zip(
            self._state._next_entity_ids_locked(len(new_cases)),
            new_cases,
            strict=True,
        ):
            stored_case = self._state._insert_case_locked(
                case_id=case_id,
                batch_id=batch.batch_id,
                source=replace(case, status="staged"),
                created_at=now_text,
            )
            if batch.program_failure_result is not None:
                stored_case.terminal_result = batch.program_failure_result

    def _validate_testcase_identities_locked(self, cases: list[CaseSpec]) -> None:
        requested: dict[int, str] = {}
        for case in cases:
            testcase_id = case.testcase_id
            if testcase_id is None:
                continue
            known_hash = requested.get(
                testcase_id, self._state._testcase_hash_by_id.get(testcase_id)
            )
            if known_hash not in {None, case.testcase_hash}:
                raise RuntimeError("DOMjudge testcase id collision")
            requested[testcase_id] = case.testcase_hash

    def activate_task_cases(self, task_id: str, *, now_text: str) -> bool:
        with self._state._lock:
            case_ids = tuple(self._state._case_ids_by_task.get(task_id, ()))
            if not case_ids:
                return False
            affected_batch_ids: set[int] = set()
            for case_id in case_ids:
                case = self._state._cases[case_id]
                if case.status == "staged":
                    inherited_result = case.terminal_result
                    if case.cancel_requested:
                        case.result = None
                        case.terminal_result = None
                        next_status = "cancelled"
                    elif inherited_result is not None:
                        case.result = inherited_result
                        case.terminal_result = None
                        next_status = "reported"
                    else:
                        next_status = "cache-pending"
                    case.cancel_requested = False
                    self._state._transition_case_locked(
                        case,
                        next_status,
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
                    affected_batch_ids.add(case.batch_id)
            self._state._refresh_batches_locked(affected_batch_ids)
            return True

    def discard_staged_task_cases(self, task_id: str, *, batch_id: int | None = None) -> int:
        """Remove a task that failed before any Case became fetchable."""

        with self._state._lock:
            case_ids = set(self._state._case_ids_by_task.get(task_id, ()))
            if any(self._state._cases[case_id].status != "staged" for case_id in case_ids):
                raise RuntimeError("cannot discard exposed judgehost task cases")
            affected_batch_ids = {self._state._cases[case_id].batch_id for case_id in case_ids}
            if batch_id is not None and int(batch_id) in self._state._empty_batch_ids:
                affected_batch_ids.add(int(batch_id))
            self._state._remove_cases_locked(case_ids)
            for batch_id in affected_batch_ids:
                if batch_id in self._state._empty_batch_ids:
                    self._state._remove_batch_locked(batch_id)
            return len(case_ids)
