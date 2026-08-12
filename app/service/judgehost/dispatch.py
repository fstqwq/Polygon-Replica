import json
import logging
import re
import time
from contextlib import nullcontext
from typing import cast
from typing import TypedDict

from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_hash_of_hashes
from app.service.judgehost.domjudge.client import domjudge_script_id
from app.service.judgehost.case_binding import CaseBinding
from app.service.judgehost.identity import domjudge_submit_id
from app.service.judgehost.shared import domjudge_lower_text, domjudge_path_name, domjudge_text
from app.service.judgehost.runtime import (
    domjudge_bool,
)
from app.service.judgehost.limits import VERIFICATION_CASE_DISPATCH_BATCH_SIZE
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate
from app.service.run.runtime import RUN_TEST_NAME_RE

from app.service.judgehost.core import JudgehostCore
from app.service.judgehost.dispatch_cache import (
    DispatchCacheMixin,
    _DomjudgeCacheEntry,
)
from app.service.judgehost.batch_scheduler_models import CompileSubmission, ExecutionBatchSpec
from app.service.judgehost.finalization import BatchFinalizationPort
from app.service.judgehost.state import JudgehostState
from app.service.judgehost.task_queue import TaskQueue
from app.service.judgehost.toolkit import DomjudgeToolkit

logger = logging.getLogger(__name__)

_DomjudgePreparedTestRow = TypedDict(
    "_DomjudgePreparedTestRow",
    {
        "name": str,
        "input_file": object,
        "answer_file": object,
    },
)

_DomjudgePrecomputedBundle = TypedDict(
    "_DomjudgePrecomputedBundle",
    {
        "compile_key": str,
        "source_hash": str,
        "compile_hash": str,
        "run_hash": str,
        "compare_hash": str,
        "compile_config": dict[str, object],
        "run_config": dict[str, object],
        "compare_config": dict[str, object],
        "compile_files": list[tuple[str, bytes, bool]],
        "run_files": list[tuple[str, bytes, bool]],
        "compare_files": list[tuple[str, bytes, bool]],
        "main_correct": bool,
    },
)

_DomjudgePreparedPayload = TypedDict(
    "_DomjudgePreparedPayload",
    {
        "source_name": str,
        "source_file": PayloadFile,
        "extra_source_items": list[tuple[str, PayloadFile]],
        "tests_rows": list[_DomjudgePreparedTestRow],
        "run_cfg_obj": dict[str, object],
        "problem_limits_obj": dict[str, object],
        "checker_args": list[str],
        "contest_id": str,
        "mode": str,
        "compile_key": str,
        "source_hash": str,
        "compile_hash": str,
        "run_hash": str,
        "compare_hash": str,
        "compile_config": dict[str, object],
        "run_config": dict[str, object],
        "compare_config": dict[str, object],
        "compile_files": list[tuple[str, bytes, bool]],
        "run_files": list[tuple[str, bytes, bool]],
        "compare_files": list[tuple[str, bytes, bool]],
        "main_correct": bool,
        "verification_source": str,
        "expected_behavior": str,
        "bypass_case_result_cache": bool,
        "manual_validate_only": bool,
        "checker_source_bytes": bytes,
        "validator_source_bytes": bytes,
        "interactor_source_bytes": bytes,
        "testlib_header_bytes": bytes,
    },
)

class DispatchHandler(DispatchCacheMixin):
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_ENQUEUING = "enqueuing"
    CASE_CACHE_KIND = DomjudgeToolkit.CASE_CACHE_KIND
    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _TASK_KIND_MAIN_CORRECT = "main-correct"
    _CACHE_PROBE_BUDGET_SEC = 0.25
    _COORDINATOR_CACHE_OWNER = "verification-coordinator-cache"

    def __init__(
        self,
        state: JudgehostState,
        core: JudgehostCore,
        queue: TaskQueue,
        batch_finalizer: BatchFinalizationPort,
        toolkit: DomjudgeToolkit,
    ) -> None:
        self._s = state
        self._core = core
        self._queue = queue
        self._batch_finalizer = batch_finalizer
        self._toolkit = toolkit

    def complete_task_exposure(
        self,
        *,
        task_id: str,
        batch_id: int,
    ) -> None:
        """Run post-bind work after staged Cases become externally visible."""

        self._queue.compact_task_payload(task_id)
        if not self._s.batch_scheduler.task_cases_terminal(task_id):
            return
        self._batch_finalizer.finalize_batch_if_ready(int(batch_id))

    def _domjudge_case_rows(
        self,
        *,
        task_id: str,
        verification_task_id: str,
        run_id: str,
        tests_rows: list[_DomjudgePreparedTestRow],
        scope_sequence: int,
    ) -> list[dict[str, object]]:
        case_rows: list[dict[str, object]] = []
        ordinal = 0
        for entry in tests_rows:
            ordinal += 1
            raw_name = domjudge_text(entry.get("name"))
            test_name = raw_name if RUN_TEST_NAME_RE.fullmatch(raw_name) else f"{ordinal:03}.in"
            input_file = PayloadFile.from_payload(entry["input_file"])
            answer_file = PayloadFile.from_payload(entry["answer_file"])
            testcase_input_hash = input_file.identity
            testcase_answer_hash = answer_file.identity
            testcase_signature = domjudge_hash_of_hashes(
                [testcase_input_hash, testcase_answer_hash]
            )
            testcase_hash = testcase_signature
            input_ref_text = (
                input_file.blob_ref
                or self._s.runtime_blob_store.put_file(input_file).blob_ref
                or ""
            )
            answer_ref_text = (
                answer_file.blob_ref
                or self._s.runtime_blob_store.put_file(answer_file).blob_ref
                or ""
            )
            testcase_id = int(testcase_hash, 16) % (1 << 63)
            case_rows.append(
                {
                    "task_id": task_id,
                    "verification_task_id": verification_task_id,
                    "run_id": run_id,
                    "test_name": test_name,
                    "ordinal": ordinal,
                    "scope_sequence": scope_sequence,
                    "testcase_id": testcase_id,
                    "testcase_hash": testcase_hash,
                    "testcase_input_hash": testcase_input_hash,
                    "testcase_answer_hash": testcase_answer_hash,
                    "input_ref": input_ref_text,
                    "answer_ref": answer_ref_text,
                    "status": "staged",
                }
            )
        return case_rows

    def domjudge_register_host(self, hostname: str) -> list[dict[str, object]]:
        safe_host = self._core.normalize_hostname(hostname)
        self._queue._record_host_event_conn(hostname=safe_host, action="register")
        release = self._s.batch_scheduler.release_host_leases(
            safe_host,
            now_text=now_iso(),
        )
        self._batch_finalizer.finalize_host_lease_release(release)
        return [
            {"jobid": job_id, "submitid": str(submit_id)}
            for job_id, submit_id in release.workdirs
        ]

    def _domjudge_task_payload(self, task: dict[str, object]) -> tuple[str, str, dict[str, object]]:
        task_id = domjudge_text(task.get("task_id"))
        if not task_id:
            raise RuntimeError("missing task_id for DOMjudge compatibility")
        run_id = domjudge_text(task.get("run_id"))
        payload = cast(dict[str, object] | None, task.get("payload"))
        if payload is None:
            raise RuntimeError("judgehost task payload is missing")
        return task_id, run_id, payload

    def _domjudge_config_object(self, raw_json: object) -> dict[str, object]:
        raw_text = domjudge_text(raw_json)
        if not raw_text:
            return {}
        try:
            return cast(dict[str, object], json.loads(raw_text))
        except Exception:
            return {}

    def _domjudge_precomputed_bundle(self, raw_precomputed: dict[str, object] | None) -> _DomjudgePrecomputedBundle | None:
        if raw_precomputed is None:
            return None
        compile_hash = domjudge_lower_text(raw_precomputed.get("compile_hash"))
        full_compile_key = domjudge_lower_text(raw_precomputed.get("compile_key"))
        run_hash = domjudge_lower_text(raw_precomputed.get("run_hash"))
        compare_hash = domjudge_lower_text(raw_precomputed.get("compare_hash"))
        source_hash = domjudge_lower_text(raw_precomputed.get("source_hash"))
        compile_config = cast(dict[str, object] | None, raw_precomputed.get("compile_config"))
        run_config = cast(dict[str, object] | None, raw_precomputed.get("run_config"))
        compare_config = cast(dict[str, object] | None, raw_precomputed.get("compare_config"))
        compile_files = cast(list[tuple[str, bytes, bool]] | None, raw_precomputed.get("compile_files"))
        run_files = cast(list[tuple[str, bytes, bool]] | None, raw_precomputed.get("run_files"))
        compare_files = cast(list[tuple[str, bytes, bool]] | None, raw_precomputed.get("compare_files"))
        main_correct = raw_precomputed.get("main_correct")
        if re.fullmatch(r"[0-9a-f]{32}", compile_hash) is None:
            return None
        if re.fullmatch(r"[0-9a-f]{64}", full_compile_key) is None:
            return None
        if re.fullmatch(r"[0-9a-f]{32}", run_hash) is None:
            return None
        if re.fullmatch(r"[0-9a-f]{32}", compare_hash) is None:
            return None
        if re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            return None
        if compile_config is None:
            return None
        if run_config is None:
            return None
        if compare_config is None:
            return None
        if compile_files is None:
            return None
        if run_files is None:
            return None
        if compare_files is None:
            return None
        if not isinstance(main_correct, bool):
            return None
        return {
            "compile_key": full_compile_key,
            "source_hash": source_hash,
            "compile_hash": compile_hash,
            "run_hash": run_hash,
            "compare_hash": compare_hash,
            "compile_config": dict(compile_config),
            "run_config": dict(run_config),
            "compare_config": dict(compare_config),
            "compile_files": list(compile_files),
            "run_files": list(run_files),
            "compare_files": list(compare_files),
            "main_correct": main_correct,
        }

    def _domjudge_prepare_payload(self, payload: dict[str, object], *, compile_only: bool) -> _DomjudgePreparedPayload:
        source_name = domjudge_path_name(payload.get("source_name"), default="submission.cpp")
        source_file = PayloadFile.from_payload(payload["source_file"])
        if source_file.size <= 0:
            raise RuntimeError("submission source payload is empty")

        extra_sources_payload = cast(dict[str, object] | None, payload.get("extra_source_files"))
        if extra_sources_payload is None:
            extra_sources_payload = {}
        extra_source_items: list[tuple[str, PayloadFile]] = []
        for raw_name, raw_file in sorted(extra_sources_payload.items()):
            safe_name = domjudge_path_name(raw_name)
            if (not safe_name) or safe_name == source_name:
                continue
            source = PayloadFile.from_payload(raw_file)
            if source.size <= 0:
                continue
            extra_source_items.append((safe_name, source))

        verification_payload = cast(dict[str, object] | None, payload.get("verification_payload"))
        if verification_payload is None:
            raise RuntimeError("verification payload is required for DOMjudge compatibility")

        tests_payload = cast(list[_DomjudgePreparedTestRow] | None, verification_payload.get("tests"))
        tests_rows = [] if tests_payload is None else list(tests_payload)
        if compile_only:
            tests_rows = [
                {
                    "name": "compile-only.in",
                    "input_file": self._s.runtime_blob_store.put_bytes(b"").to_payload(),
                    "answer_name": "compile-only.ans",
                    "answer_file": self._s.runtime_blob_store.put_bytes(b"").to_payload(),
                }
            ]
        if not tests_rows:
            raise RuntimeError("no tests in judgehost payload")

        run_cfg_obj = self._domjudge_config_object(verification_payload.get("run_config_json"))
        problem_limits_obj = cast(dict[str, object] | None, verification_payload.get("problem_limits"))
        if problem_limits_obj is None:
            problem_limits_obj = {}

        checker_args: list[str] = []
        checker_args_raw = cast(list[object] | None, run_cfg_obj.get("checker_args"))
        if checker_args_raw is not None:
            for item in checker_args_raw:
                token = domjudge_text(item)
                if token:
                    checker_args.append(token)

        precomputed = self._domjudge_precomputed_bundle(payload.get("domjudge_precomputed"))
        if precomputed is None:
            raise RuntimeError("domjudge precomputed payload is required")

        return {
            "source_name": source_name,
            "source_file": source_file,
            "extra_source_items": extra_source_items,
            "tests_rows": tests_rows,
            "run_cfg_obj": run_cfg_obj,
            "problem_limits_obj": problem_limits_obj,
            "checker_args": checker_args,
            "contest_id": "local",
            "mode": domjudge_lower_text(payload.get("mode"), default="pass-fail"),
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
            "main_correct": precomputed["main_correct"],
            "verification_source": domjudge_lower_text(payload.get("verification_source"))
            or self._toolkit.task_kind(payload),
            "expected_behavior": domjudge_lower_text(payload.get("expected_behavior")),
            "bypass_case_result_cache": domjudge_bool(payload.get("bypass_case_result_cache"), default=False),
            "manual_validate_only": domjudge_bool(payload.get("manual_validate_only"), default=False),
        }

    def _domjudge_cache_entry(self, raw_entry: dict[str, object] | None) -> _DomjudgeCacheEntry | None:
        if raw_entry is None:
            return None
        cache_value = cast(dict[str, object] | None, raw_entry.get("value"))
        if cache_value is None:
            cache_value = {}
        cache_files = cast(dict[str, object] | None, raw_entry.get("files"))
        if cache_files is None:
            cache_files = {}
        return {
            "value": dict(cache_value),
            "files": dict(cache_files),
        }

    def _domjudge_stage_task(self, task: dict[str, object], *, execution_signature: str = "") -> int:
        task_id, run_id, payload = self._domjudge_task_payload(task)
        latest_task_row = self._core.task_by_id(task_id)
        if latest_task_row is not None:
            payload = dict(latest_task_row.get("payload") or {})
            run_id = domjudge_text(latest_task_row.get("run_id"), default=run_id)
        prepared = self._domjudge_prepare_payload(
            payload,
            compile_only=self._toolkit.task_kind(payload) == self._TASK_KIND_COMPILE_ONLY,
        )
        source_name = prepared["source_name"]
        source_file = prepared["source_file"]
        extra_source_items = prepared["extra_source_items"]
        contest_id = prepared["contest_id"]
        mode = prepared["mode"]
        compile_hash = prepared["compile_hash"]
        full_compile_key = prepared["compile_key"]
        run_hash = prepared["run_hash"]
        compare_hash = prepared["compare_hash"]
        source_hash = prepared["source_hash"]
        compile_config = prepared["compile_config"]
        run_config = prepared["run_config"]
        compare_config = prepared["compare_config"]
        tests_rows = prepared["tests_rows"]
        expected_behavior = prepared["expected_behavior"]
        verification_source = prepared["verification_source"]
        bypass_case_result_cache = prepared["bypass_case_result_cache"]
        compile_files = prepared["compile_files"]
        run_files = prepared["run_files"]
        compare_files = prepared["compare_files"]

        now_text = now_iso()
        verification_id = domjudge_text(payload.get("verification_id"))
        if not verification_id:
            raise RuntimeError("execution scope id is required")
        verification_program_id = domjudge_text(
            payload.get("verification_program_id")
        )
        task_kind = self._toolkit.task_kind(payload)
        scope_sequence = self._s.batch_scheduler.scope_sequence(verification_id)
        case_rows = self._domjudge_case_rows(
            task_id=task_id,
            verification_task_id=domjudge_text(payload.get("verification_task_id")),
            run_id=run_id,
            tests_rows=tests_rows,
            scope_sequence=scope_sequence,
        )
        service_class = domjudge_lower_text(payload.get("service_class"), default="background")
        if service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        compile_submission = CompileSubmission(
            compile_key=full_compile_key,
            submit_id=domjudge_submit_id(full_compile_key),
            source_name=source_name,
            source_file=source_file,
            extra_source_items=tuple(extra_source_items),
            compile_files=tuple(compile_files),
        )
        batch_spec = ExecutionBatchSpec(
            run_files=tuple(run_files),
            compare_files=tuple(compare_files),
        )
        return self._s.batch_scheduler.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            verification_program_id=verification_program_id,
            execution_signature=execution_signature,
            task_kind=task_kind,
            verification_id=verification_id,
            compile_key=full_compile_key,
            compile_submission=compile_submission,
            contest_id=contest_id,
            mode=mode,
            source_name=source_name,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            source_hash=source_hash,
            compile_config_json=json.dumps(compile_config, ensure_ascii=False, separators=(",", ":")),
            run_config_json=json.dumps(run_config, ensure_ascii=False, separators=(",", ":")),
            compare_config_json=json.dumps(compare_config, ensure_ascii=False, separators=(",", ":")),
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            bypass_case_result_cache=1 if bypass_case_result_cache else 0,
            service_class=service_class,
            batch_spec=batch_spec,
            created_at=now_text,
            case_rows=case_rows,
        )

    def stage_task(self, task: dict[str, object]) -> int:
        _task_id, _run_id, payload = self._domjudge_task_payload(task)
        execution_signature = domjudge_text(payload.get("domjudge_execution_signature"))
        return self._domjudge_stage_task(task, execution_signature=execution_signature)

    def finalize_batch_if_ready(self, batch_id: int, *, error_text: str = "") -> None:
        self._batch_finalizer.finalize_batch_if_ready(
            int(batch_id),
            force_failed=bool(error_text),
            error_text=error_text,
        )

    def activate_task_cases(self, task_id: str) -> bool:
        return self._s.batch_scheduler.activate_task_cases(
            task_id,
            now_text=now_iso(),
        )

    def _domjudge_lease_cases(self, batch_id: int, hostname: str, max_batchsize: int) -> list[dict[str, object]]:
        now_text = now_iso()
        batch_row = self._s.batch_scheduler.fetch_batch(int(batch_id))
        if batch_row is None:
            return []
        cap = max(1, min(256, int(max_batchsize)))
        if domjudge_lower_text(batch_row["status"]) != "open":
            return []
        rows = self._s.batch_scheduler.lease_cases(
            int(batch_id),
            hostname=hostname,
            limit=int(cap),
            now_text=now_text,
        )
        if not rows:
            return []
        compile_hash = domjudge_lower_text(batch_row["compile_hash"])
        run_hash = domjudge_lower_text(batch_row["run_hash"])
        compare_hash = domjudge_lower_text(batch_row["compare_hash"])
        compile_id = int(domjudge_script_id(compile_hash))
        run_id_num = int(domjudge_script_id(run_hash))
        compare_id = int(domjudge_script_id(compare_hash))
        compile_submission = self._s.batch_scheduler.source_submission(
            str(domjudge_submit_id(domjudge_text(batch_row["compile_key"])))
        )
        if compile_submission is None:
            raise RuntimeError("compile submission disappeared")
        contest_id = domjudge_text(batch_row["contest_id"]) or "local"
        compile_config_json = domjudge_text(batch_row["compile_config_json"])
        run_config_json = domjudge_text(batch_row["run_config_json"])
        compare_config_json = domjudge_text(batch_row["compare_config_json"])
        out: list[dict[str, object]] = []
        for row in rows:
            case_id = int(row["id"])
            out.append(
                {
                    "type": "judging_run",
                    "judgetaskid": case_id,
                    "jobid": int(batch_row["domjudge_job_id"]),
                    "uuid": domjudge_text(batch_row["compile_key"]),
                    "submitid": str(compile_submission.submit_id),
                    "contestid": contest_id,
                    "compile_script_id": str(int(compile_id)),
                    "run_script_id": str(int(run_id_num)),
                    "compare_script_id": str(int(compare_id)),
                    "testcase_id": str(int(row["testcase_id"])),
                    "testcase_hash": domjudge_text(row["testcase_hash"]),
                    "compile_config": compile_config_json,
                    "run_config": run_config_json,
                    "compare_config": compare_config_json,
                }
            )
        for case_task_id in dict.fromkeys(domjudge_text(row["task_id"]) for row in rows):
            if case_task_id:
                self._s.task_registry.transition(
                    case_task_id,
                    expected={self.STATUS_QUEUED},
                    status=self.STATUS_LEASED,
                    updates={"updated_at": now_text},
                )
        for row in rows:
            case_task_id = domjudge_text(row["task_id"])
            verification_task_id = domjudge_text(
                row["verification_task_id"]
            )
            task_row = self._core.task_by_id(case_task_id)
            verification_id = "" if task_row is None else domjudge_text(task_row.get("verification_id"))
            if verification_id and verification_task_id:
                self._s.execution_port.case_leased(
                    CaseBinding(
                        execution_scope_id=verification_id,
                        program_id=domjudge_text(
                            batch_row["verification_program_id"]
                        ),
                        task_id=verification_task_id,
                        test_name=domjudge_text(row["test_name"]),
                    )
                )
        self._s.batch_scheduler.record_batch_leased(
            hostname,
            int(batch_id),
            [int(row["id"]) for row in rows],
            leased_monotonic=time.monotonic(),
        )
        first_row = rows[0]
        self._queue._record_host_event_conn(
            hostname=hostname,
            action="lease",
            task_id=domjudge_text(first_row["task_id"]),
            run_id=domjudge_text(first_row["run_id"]),
        )
        return out

    def domjudge_fetch_work(
        self,
        hostname: str,
        max_batchsize: int | None = None,
        *,
        admission_gate: MaintenanceAdmissionGate | None = None,
    ) -> list[dict[str, object]]:
        safe_host = self._core.normalize_hostname(hostname)
        policy = self._s.config_policy()
        cap = (
            policy.fetch_batch_size
            if max_batchsize is None
            else max(1, min(256, int(max_batchsize)))
        )
        deadline = time.monotonic() + self._CACHE_PROBE_BUDGET_SEC
        first_transition = True
        long_poll_used = False
        initialized = False
        while first_transition or time.monotonic() < deadline:
            first_transition = False
            admission_scope = (
                nullcontext(True)
                if admission_gate is None
                else admission_gate.try_locked()
            )
            with admission_scope as admission_acquired:
                if (
                    not admission_acquired
                    or (
                        admission_gate is not None
                        and not admission_gate.is_open_locked()
                    )
                ):
                    return []
                if not initialized:
                    initialized = True
                    if not self._queue._host_enabled_conn(hostname=safe_host):
                        self._queue._record_host_event_conn(
                            hostname=safe_host,
                            action="disabled",
                        )
                        return []
                    self._batch_finalizer.retry_due_finalizations(limit=1)
                batch_row = self._s.batch_scheduler.select_ready_batch(safe_host)
                if batch_row is not None:
                    batch_id = int(batch_row["batch_id"])
                    processed = self._domjudge_apply_cache_shortcuts_for_batch(
                        batch_id,
                        hostname=safe_host,
                        limit=VERIFICATION_CASE_DISPATCH_BATCH_SIZE,
                        deadline=deadline,
                    )
                    refreshed = self._s.batch_scheduler.fetch_batch(batch_id)
                    if (
                        refreshed is None
                        or domjudge_lower_text(refreshed["status"]) != "open"
                    ):
                        continue
                    needs_materialization = (
                        domjudge_lower_text(refreshed["materialization_state"])
                        != "ready"
                        and self._s.batch_scheduler.batch_case_count(
                            batch_id,
                            status="pending",
                        )
                        > 0
                    )
                    if (
                        needs_materialization
                        and not self._domjudge_materialize_batch(batch_id)
                    ):
                        return []
                    leased_cases = self._domjudge_lease_cases(
                        batch_id,
                        safe_host,
                        cap,
                    )
                    if leased_cases:
                        return leased_cases
                    self._batch_finalizer.finalize_batch_if_ready(batch_id)
                    if processed == 0:
                        return []
                    continue

            if not long_poll_used:
                long_poll_used = True
                if self._s.batch_scheduler.wait_for_ready_batch(
                    self._s.fetch_long_poll_sec
                ):
                    deadline = time.monotonic() + self._CACHE_PROBE_BUDGET_SEC
                    first_transition = True
                    continue
            admission_scope = (
                nullcontext(True)
                if admission_gate is None
                else admission_gate.try_locked()
            )
            with admission_scope as admission_acquired:
                if (
                    not admission_acquired
                    or (
                        admission_gate is not None
                        and not admission_gate.is_open_locked()
                    )
                ):
                    return []
                self._queue._record_host_event_conn(
                    hostname=safe_host,
                    action="fetch",
                )
            return []
        return []

    def probe_task_case_cache(self, task_ids: list[str]) -> set[str]:
        remaining = VERIFICATION_CASE_DISPATCH_BATCH_SIZE
        ordered_task_ids = list(dict.fromkeys(task_ids))
        batch_ids: dict[int, None] = {}
        for task_id in ordered_task_ids:
            batch = self._s.batch_scheduler.batch_for_task(task_id)
            if batch is None:
                continue
            batch_id = int(batch["batch_id"])
            batch_ids.setdefault(batch_id, None)
        for batch_id in batch_ids:
            if remaining <= 0:
                break
            processed = self._domjudge_apply_cache_shortcuts_for_batch(
                batch_id,
                hostname=self._COORDINATOR_CACHE_OWNER,
                limit=remaining,
                deadline=None,
            )
            remaining -= processed
        return {
            task_id
            for task_id in ordered_task_ids
            if self._s.batch_scheduler.task_has_cache_pending_cases(task_id)
        }
