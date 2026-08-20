import logging
import re
import time
from contextlib import nullcontext

from app.db import now_iso
from app.service.judgehost.domjudge.identity import script_id
from app.service.judgehost.ports.case_binding import CaseBinding
from app.service.judgehost.domjudge.identity import submit_id
from app.service.judgehost.domjudge.codec import decode_json_object, decode_text
from app.service.judgehost.domjudge.result import (
    parse_bool,
)
from app.service.judgehost.domjudge.scripts import DomjudgeScriptCatalog
from app.service.judgehost.domjudge.limits import VERIFICATION_CASE_DISPATCH_BATCH_SIZE
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate

from app.service.judgehost.validation import normalize_judgehost_hostname
from app.service.judgehost.configuration import (
    JudgehostConfiguration,
    JudgehostSettings,
)
from app.service.judgehost.host.registry import JudgehostHostRegistry
from app.service.judgehost.cache.case_result import CaseCacheLookup, CaseResultCache
from app.service.judgehost.dispatch.materializer import (
    BatchPayloadMaterializer,
    MaterializationRequest,
)
from app.service.judgehost.dispatch.model import (
    CacheProbeOutcome,
    DispatchOutcome,
    HostRegistrationOutcome,
)
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.batch.model import ExecutionBatchRow, LeaseClaim
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.ports.completion import CaseLeaseSink

logger = logging.getLogger(__name__)

class JudgehostDispatch:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_ENQUEUING = "enqueuing"
    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _TASK_KIND_MAIN_CORRECT = "main-correct"
    _CACHE_PROBE_BUDGET_SEC = 0.25
    _COORDINATOR_CACHE_OWNER = "verification-coordinator-cache"

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        tasks: JudgehostTaskRegistry,
        execution_port: CaseLeaseSink,
        scripts: DomjudgeScriptCatalog,
        case_result_cache: CaseResultCache,
        materializer: BatchPayloadMaterializer,
        configuration: JudgehostConfiguration,
        hosts: JudgehostHostRegistry,
        fetch_long_poll_sec: float,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._tasks = tasks
        self._execution_port = execution_port
        self._scripts = scripts
        self._case_result_cache = case_result_cache
        self._materializer = materializer
        self._configuration = configuration
        self._hosts = hosts
        self._fetch_long_poll_sec = fetch_long_poll_sec

    def domjudge_register_host(self, hostname: str) -> HostRegistrationOutcome:
        safe_host = normalize_judgehost_hostname(hostname)
        self._hosts.record_contact(hostname=safe_host)
        release = self._batch_runtime.release_host_leases(
            safe_host,
            now_text=now_iso(),
        )
        return HostRegistrationOutcome(
            workdirs=tuple(
                {"jobid": job_id, "submitid": str(submit_id)}
                for job_id, submit_id in release.workdirs
            ),
            terminal_batch_ids=release.terminal_batch_ids,
        )

    def _try_cache_shortcut(
        self,
        *,
        batch_row,
        case_row,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
    ):
        testcase_hash = case_row["testcase_hash"]
        if not testcase_hash:
            raise RuntimeError(
                f"missing testcase_hash for DOMjudge case {int(case_row['id'])}"
            )
        if not case_row["testcase_input_hash"]:
            raise RuntimeError(
                f"missing testcase_input_hash for DOMjudge case {int(case_row['id'])}"
            )
        if not case_row["testcase_answer_hash"]:
            raise RuntimeError(
                f"missing testcase_answer_hash for DOMjudge case {int(case_row['id'])}"
            )
        expected_behavior = batch_row["expected_behavior"] or "unknown"
        verification_source = batch_row["verification_source"]
        main_correct = verification_source == self._TASK_KIND_MAIN_CORRECT
        return self._case_result_cache.lookup(
            CaseCacheLookup(
                source_hash=batch_row["source_hash"],
                compile_hash=batch_row["compile_hash"],
                run_hash=batch_row["run_hash"],
                compare_hash=batch_row["compare_hash"],
                compile_config_hash=compile_config_hash,
                run_config_hash=run_config_hash,
                compare_config_hash=compare_config_hash,
                toolchain_cmd_digest=toolchain_cmd_digest,
                testcase_hash=testcase_hash,
                run_config=decode_json_object(batch_row["run_config_json"]),
                expected_behavior=expected_behavior,
                main_correct=main_correct,
                requires_output=(main_correct or "generate-input" in verification_source),
                bypass=parse_bool(
                    batch_row["bypass_case_result_cache"], default=False
                ),
            )
        )

    def _materialize_batch(
        self,
        batch_id: int,
        *,
        admission_gate: MaintenanceAdmissionGate | None,
    ) -> bool:
        batch_row = self._batch_runtime.fetch_batch(batch_id)
        if batch_row is None or batch_row["status"] != "open":
            return False
        if batch_row["materialization_state"] == "ready":
            return True
        if batch_row["materialization_state"] == "failed":
            return False
        admission_scope = (
            nullcontext(True) if admission_gate is None else admission_gate.try_locked()
        )
        with admission_scope as admission_acquired:
            if not admission_acquired or (
                admission_gate is not None
                and not admission_gate.allows_runtime_work_locked()
            ):
                return False
            claim = self._batch_runtime.claim_materialization(
                batch_id,
                now_text=now_iso(),
            )
        if claim is None:
            refreshed = self._batch_runtime.fetch_batch(batch_id)
            return bool(
                refreshed is not None
                and refreshed["materialization_state"] == "ready"
            )
        try:
            materialized = self._materializer.materialize(
                MaterializationRequest(
                    batch=claim.batch,
                    spec=claim.spec,
                    submission=claim.submission,
                )
            )
        except Exception as exc:
            error_text = f"judgehost materialization failed: {exc}"
            self._batch_runtime.finish_materialization(
                claim,
                success=False,
                materialized_submission=None,
                error_text=error_text,
                now_text=now_iso(),
            )
            return False
        return self._batch_runtime.finish_materialization(
            claim,
            success=True,
            materialized_submission=materialized.submission,
            error_text="",
            now_text=now_iso(),
        )

    def _apply_cache_shortcuts_for_batch(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
        deadline: float | None,
        admission_gate: MaintenanceAdmissionGate | None,
        settings: JudgehostSettings,
    ) -> int:
        batch_row = self._batch_runtime.fetch_batch(batch_id)
        if batch_row is None:
            return 0
        compile_cfg = decode_json_object(batch_row["compile_config_json"])
        run_cfg = decode_json_object(batch_row["run_config_json"])
        compare_cfg = decode_json_object(batch_row["compare_config_json"])
        compile_config_hash = RuntimeCacheIndex.signature(compile_cfg)
        run_config_hash = RuntimeCacheIndex.signature(run_cfg)
        compare_config_hash = RuntimeCacheIndex.signature(compare_cfg)
        toolchain_cmd_digest = decode_text(raw=compile_cfg.get("toolchain_cmd_digest"))
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._scripts.toolchain_cmd_digest(
                settings,
                batch_row["source_name"]
            )
        admission_scope = (
            nullcontext(True) if admission_gate is None else admission_gate.try_locked()
        )
        with admission_scope as admission_acquired:
            if not admission_acquired or (
                admission_gate is not None
                and not admission_gate.allows_runtime_work_locked()
            ):
                return 0
            claims = self._batch_runtime.claim_cache_cases(
                batch_id,
                hostname=hostname,
                limit=max(0, limit),
                now_text=now_iso(),
            )
        if not claims:
            return 0
        processed = 0
        finished = []
        unprocessed = []
        for claim, row in claims:
            if deadline is not None and processed > 0 and time.monotonic() >= deadline:
                unprocessed.append(claim)
                continue
            try:
                shortcut = self._try_cache_shortcut(
                    batch_row=batch_row,
                    case_row=row,
                    compile_config_hash=compile_config_hash,
                    run_config_hash=run_config_hash,
                    compare_config_hash=compare_config_hash,
                    toolchain_cmd_digest=toolchain_cmd_digest,
                )
            except Exception:
                logger.exception(
                    "judgehost result-cache probe failed batch_id=%s case_id=%s",
                    batch_id,
                    row["id"],
                )
                shortcut = None
            finished.append((claim, shortcut))
            processed += 1
        self._batch_runtime.finish_cache_claims(finished, updated_at=now_iso())
        if unprocessed:
            self._batch_runtime.abort_cache_claims(unprocessed, updated_at=now_iso())
        return processed

    def _lease_cases(
        self,
        batch_id: int,
        hostname: str,
        max_batchsize: int,
        *,
        admission_gate: MaintenanceAdmissionGate | None,
    ) -> list[dict[str, object]]:
        now_text = now_iso()
        batch_row = self._batch_runtime.fetch_batch(int(batch_id))
        if batch_row is None:
            return []
        cap = max(1, min(256, int(max_batchsize)))
        if batch_row["status"] != "open":
            return []
        admission_scope = (
            nullcontext(True) if admission_gate is None else admission_gate.try_locked()
        )
        with admission_scope as admission_acquired:
            if not admission_acquired or (
                admission_gate is not None
                and not admission_gate.allows_runtime_work_locked()
            ):
                return []
            claim = self._batch_runtime.claim_lease(
                int(batch_id),
                hostname=hostname,
                limit=int(cap),
                now_text=now_text,
                lease_grace_sec=self._configuration.snapshot().online_window_sec,
            )
        if claim is None:
            return []
        rows = claim.cases
        leased_task_ids: list[str] = []
        try:
            out = self._project_lease_work(batch_row, claim)
            for row in rows:
                case_task_id = row["task_id"]
                verification_task_id = row["verification_task_id"]
                task_row = self._tasks.get(case_task_id)
                verification_id = (
                    "" if task_row is None else decode_text(raw=task_row.get("verification_id"))
                )
                if verification_id and verification_task_id:
                    accepted = self._execution_port.case_leased(
                        CaseBinding(
                            execution_scope_id=verification_id,
                            program_id=batch_row["verification_program_id"],
                            task_id=verification_task_id,
                            test_name=row["test_name"],
                        )
                    )
                    if not accepted:
                        raise RuntimeError(
                            "verification task rejected its Judgehost lease"
                        )
            for case_task_id in dict.fromkeys(row["task_id"] for row in rows):
                if not case_task_id:
                    continue
                task_lease = self._tasks.mark_leased(
                    case_task_id,
                    now_text=now_text,
                )
                if task_lease == "rejected":
                    raise RuntimeError("judgehost task rejected its Case lease")
                if task_lease == "claimed":
                    leased_task_ids.append(case_task_id)
            lease_start = time.monotonic()
            if not self._batch_runtime.commit_lease(
                claim,
                leased_monotonic=lease_start,
            ):
                raise RuntimeError("judgehost lease claim became stale")
            self._batch_runtime.record_batch_leased(
                hostname,
                int(batch_id),
                [int(row["id"]) for row in rows],
                leased_monotonic=lease_start,
            )
            self._hosts.record_contact(hostname=hostname)
            return out
        except Exception:
            self._batch_runtime.abort_lease(claim, now_text=now_iso())
            for task_id in leased_task_ids:
                self._tasks.abort_lease(task_id, now_text=now_iso())
            raise

    def _project_lease_work(
        self,
        batch_row: ExecutionBatchRow,
        claim: LeaseClaim,
    ) -> list[dict[str, object]]:
        compile_id = script_id(batch_row["compile_hash"])
        run_id_num = script_id(batch_row["run_hash"])
        compare_id = script_id(batch_row["compare_hash"])
        submission = self._batch_runtime.source_submission(
            str(submit_id(batch_row["compile_key"]))
        )
        if submission is None:
            raise RuntimeError("compile submission disappeared")
        out: list[dict[str, object]] = []
        for row in claim.cases:
            testcase_id = row["testcase_id"]
            if testcase_id is None:
                raise RuntimeError("leased judgehost case has no testcase id")
            out.append(
                {
                    "type": "judging_run",
                    "judgetaskid": int(row["id"]),
                    "jobid": int(batch_row["job_id"]),
                    "uuid": batch_row["compile_key"],
                    "submitid": str(submission.submit_id),
                    "contestid": batch_row["contest_id"] or "local",
                    "compile_script_id": str(compile_id),
                    "run_script_id": str(run_id_num),
                    "compare_script_id": str(compare_id),
                    "testcase_id": str(testcase_id),
                    "testcase_hash": row["testcase_hash"],
                    "compile_config": batch_row["compile_config_json"],
                    "run_config": batch_row["run_config_json"],
                    "compare_config": batch_row["compare_config_json"],
                }
            )
        return out

    def domjudge_fetch_work(
        self,
        hostname: str,
        max_batchsize: int | None = None,
        *,
        admission_gate: MaintenanceAdmissionGate | None = None,
    ) -> DispatchOutcome:
        safe_host = normalize_judgehost_hostname(hostname)
        policy = self._configuration.snapshot()
        cap = (
            policy.fetch_batch_size
            if max_batchsize is None
            else max(1, min(256, int(max_batchsize)))
        )
        deadline = time.monotonic() + self._CACHE_PROBE_BUDGET_SEC
        first_transition = True
        long_poll_used = False
        initialized = False
        affected_batch_ids: dict[int, None] = {}
        while first_transition or time.monotonic() < deadline:
            first_transition = False
            admission_scope = (
                nullcontext(True) if admission_gate is None else admission_gate.try_locked()
            )
            batch_row = None
            with admission_scope as admission_acquired:
                if not admission_acquired or (
                    admission_gate is not None and not admission_gate.allows_runtime_work_locked()
                ):
                    return self._outcome((), affected_batch_ids)
                if not initialized:
                    initialized = True
                    if not self._hosts.enabled(safe_host):
                        self._hosts.record_contact(hostname=safe_host)
                        return self._outcome((), affected_batch_ids)
                batch_row = self._batch_runtime.select_ready_batch(safe_host)

            if batch_row is not None:
                batch_id = int(batch_row["batch_id"])
                affected_batch_ids[batch_id] = None
                processed = self._apply_cache_shortcuts_for_batch(
                    batch_id,
                    hostname=safe_host,
                    limit=VERIFICATION_CASE_DISPATCH_BATCH_SIZE,
                    deadline=deadline,
                    admission_gate=admission_gate,
                    settings=policy,
                )
                refreshed = self._batch_runtime.fetch_batch(batch_id)
                if refreshed is None or refreshed["status"] != "open":
                    continue
                needs_materialization = (
                    refreshed["materialization_state"] != "ready"
                    and self._batch_runtime.batch_case_count(
                        batch_id,
                        status="pending",
                    )
                    > 0
                )
                if needs_materialization and not self._materialize_batch(
                    batch_id,
                    admission_gate=admission_gate,
                ):
                    return self._outcome((), affected_batch_ids)
                leased_cases = self._lease_cases(
                    batch_id,
                    safe_host,
                    cap,
                    admission_gate=admission_gate,
                )
                if leased_cases:
                    return self._outcome(tuple(leased_cases), affected_batch_ids)
                if processed == 0:
                    return self._outcome((), affected_batch_ids)
                continue

            if admission_gate is not None and admission_gate.state() == "draining":
                return self._outcome((), affected_batch_ids)
            if not long_poll_used:
                long_poll_used = True
                if self._batch_runtime.wait_for_ready_batch(self._fetch_long_poll_sec):
                    deadline = time.monotonic() + self._CACHE_PROBE_BUDGET_SEC
                    first_transition = True
                    continue
            admission_scope = (
                nullcontext(True) if admission_gate is None else admission_gate.try_locked()
            )
            with admission_scope as admission_acquired:
                if not admission_acquired or (
                    admission_gate is not None and not admission_gate.allows_runtime_work_locked()
                ):
                    return self._outcome((), affected_batch_ids)
                self._hosts.record_contact(hostname=safe_host)
            return self._outcome((), affected_batch_ids)
        return self._outcome((), affected_batch_ids)

    @staticmethod
    def _outcome(
        work: tuple[dict[str, object], ...],
        batch_ids: dict[int, None],
    ) -> DispatchOutcome:
        return DispatchOutcome(
            work=work,
            terminal_batch_ids=tuple(batch_ids),
        )

    def probe_task_case_cache(self, task_ids: list[str]) -> CacheProbeOutcome:
        settings = self._configuration.snapshot()
        remaining = VERIFICATION_CASE_DISPATCH_BATCH_SIZE
        ordered_task_ids = list(dict.fromkeys(task_ids))
        batch_ids: dict[int, None] = {}
        for task_id in ordered_task_ids:
            batch = self._batch_runtime.batch_for_task(task_id)
            if batch is None:
                continue
            batch_id = int(batch["batch_id"])
            batch_ids.setdefault(batch_id, None)
        affected_batch_ids: list[int] = []
        for batch_id in batch_ids:
            if remaining <= 0:
                break
            affected_batch_ids.append(batch_id)
            processed = self._apply_cache_shortcuts_for_batch(
                batch_id,
                hostname=self._COORDINATOR_CACHE_OWNER,
                limit=remaining,
                deadline=None,
                admission_gate=None,
                settings=settings,
            )
            remaining -= processed
        return CacheProbeOutcome(
            pending_task_ids=frozenset(
                task_id
                for task_id in ordered_task_ids
                if self._batch_runtime.task_has_cache_pending_cases(task_id)
            ),
            terminal_batch_ids=tuple(affected_batch_ids),
        )
