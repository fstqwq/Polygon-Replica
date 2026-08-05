from __future__ import annotations

import logging
import re
import time
from typing import TypedDict

from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_json_hash
from app.service.judgehost.runtime import (
    domjudge_bool,
    domjudge_parse_float,
    domjudge_verdict_from_runresult,
    domjudge_rewrite_untrusted_runresult,
)
from app.service.judgehost.shared import domjudge_lower_text, domjudge_text

from app.service.judgehost.batch_scheduler_models import (
    CaseClaim,
    CaseResult,
    CompileSubmission,
    ExecutionBatchRow,
    JudgehostCaseRow,
)

logger = logging.getLogger(__name__)
_DomjudgeCacheEntry = TypedDict(
    "_DomjudgeCacheEntry",
    {
        "value": dict[str, object],
        "files": dict[str, object],
    },
)

class DispatchCacheMixin:
    """Resolve result-cache hits and materialize execution batches on misses."""

    def _domjudge_try_cache_shortcut(
        self,
        *,
        batch_row: ExecutionBatchRow,
        case_row: JudgehostCaseRow,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
    ) -> CaseResult | None:
        source_hash = domjudge_lower_text(batch_row["source_hash"])
        compile_hash = domjudge_lower_text(batch_row["compile_hash"])
        run_hash = domjudge_lower_text(batch_row["run_hash"])
        compare_hash = domjudge_lower_text(batch_row["compare_hash"])
        testcase_hash = domjudge_lower_text(case_row["testcase_hash"])
        testcase_input_hash = domjudge_lower_text(case_row["testcase_input_hash"])
        testcase_answer_hash = domjudge_lower_text(case_row["testcase_answer_hash"])
        if not testcase_hash:
            raise RuntimeError(f"missing testcase_hash for DOMjudge case {int(case_row['id'])}")
        if not testcase_input_hash:
            raise RuntimeError(f"missing testcase_input_hash for DOMjudge case {int(case_row['id'])}")
        if not testcase_answer_hash:
            raise RuntimeError(f"missing testcase_answer_hash for DOMjudge case {int(case_row['id'])}")

        bypass_case_result_cache = domjudge_bool(batch_row["bypass_case_result_cache"], default=False)
        expected_behavior = domjudge_lower_text(batch_row["expected_behavior"], default="unknown")
        verification_source = domjudge_lower_text(batch_row["verification_source"])
        main_correct = verification_source == self._TASK_KIND_MAIN_CORRECT
        compile_only = expected_behavior == "compile"

        case_key_hash, case_signature = self._toolkit.case_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            compare_config_hash=compare_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_hash=testcase_hash,
        )
        if bypass_case_result_cache:
            self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
            return None

        run_cfg_obj = self._domjudge_config_object(batch_row["run_config_json"])

        blob_names = [
            "program.err",
            "judgemessage.txt",
            "teammessage.txt",
            "compare.meta",
        ]
        requires_output_blob = main_correct or "generate-input" in verification_source
        if requires_output_blob:
            blob_names.append("program.out")
        cached_read = self._toolkit.cache_get_with_payloads(
            self.CASE_CACHE_KIND,
            case_key_hash,
            case_signature,
            names=blob_names,
        )
        if cached_read is None:
            return None
        cached_entry, cached_payloads = cached_read
        cached_exact = self._domjudge_cache_entry(cached_entry)
        if cached_exact is not None:
            cached_obj = cached_exact["value"]
            if not domjudge_bool(cached_obj.get("shortcut_eligible"), default=True):
                return None
            cached_runresult = domjudge_text(cached_obj.get("runresult"))
            cached_runresult = domjudge_rewrite_untrusted_runresult(
                cached_runresult,
                cpu_sec=domjudge_parse_float(
                    cached_obj.get("cpu_sec"),
                    domjudge_parse_float(cached_obj.get("runtime_sec"), 0.0),
                ),
                run_cfg_obj=run_cfg_obj,
            )
            cached_verdict = domjudge_verdict_from_runresult(cached_runresult)
            if cached_verdict == "FL":
                self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            # Build answer generation, expected accepted runs, and compile-only tasks
            # must not reuse non-OK cached outcomes; otherwise transient failures can
            # poison later requests.
            if (main_correct or expected_behavior in {"accepted", "compile"}) and cached_verdict != "OK":
                if expected_behavior == "compile":
                    self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            built = self._toolkit.build_cached_case(
                cache_value=cached_obj,
                cache_files=cached_exact["files"],
            )
            cached_result = self._domjudge_cached_case_bundle(
                test_name=domjudge_text(case_row["test_name"]),
                runresult=cached_runresult,
                built=built,
                payloads=cached_payloads,
            )
            if cached_verdict == "OK" and (not compile_only):
                # Cached OK result must carry a resolvable output artifact.
                if not cached_result.output_run_ref:
                    self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                    return None
            if requires_output_blob and "program.out" not in cached_payloads:
                self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            return cached_result

        return None

    def _domjudge_materialize_batch(self, batch_id: int) -> bool:
        batch_row = self._s.batch_scheduler.fetch_batch(int(batch_id))
        if batch_row is None or domjudge_lower_text(batch_row["status"]) != "open":
            return False
        state = domjudge_lower_text(batch_row["materialization_state"])
        if state == "ready":
            return True
        if state == "failed":
            return False
        if not self._s.batch_scheduler.claim_materialization(int(batch_id), now_text=now_iso()):
            refreshed = self._s.batch_scheduler.fetch_batch(int(batch_id))
            return bool(refreshed is not None and refreshed["materialization_state"] == "ready")
        spec = self._s.batch_scheduler.batch_spec(int(batch_id))
        submission = self._s.batch_scheduler.compile_submission_for_batch(int(batch_id))
        if spec is None or submission is None:
            self._s.batch_scheduler.finish_materialization(
                int(batch_id),
                success=False,
                error_text="judgehost batch specification disappeared",
                now_text=now_iso(),
            )
            self._result._domjudge_finalize_batch_if_ready(
                int(batch_id),
                force_failed=True,
                error_text="judgehost batch specification disappeared",
            )
            return False
        try:
            materialized_submission = CompileSubmission(
                compile_key=submission.compile_key,
                submit_id=submission.submit_id,
                source_name=submission.source_name,
                source_file=self._s.runtime_blob_store.put_file(submission.source_file),
                extra_source_items=tuple(
                    (name, self._s.runtime_blob_store.put_file(payload))
                    for name, payload in submission.extra_source_items
                ),
                compile_files=submission.compile_files,
            )
            self._s.batch_scheduler.publish_materialized_compile_submission(
                submission.compile_key,
                materialized_submission,
            )
            for kind, hash_key, files_key in (
                ("run", "run_hash", "run_files"),
                ("compare", "compare_hash", "compare_files"),
            ):
                self._toolkit.store_executable_cache(
                    kind=kind,
                    executable_hash=domjudge_text(batch_row[hash_key]),
                    files=list(getattr(spec, files_key)),
                )
            self._toolkit.store_executable_cache(
                kind="compile",
                executable_hash=domjudge_text(batch_row["compile_hash"]),
                files=list(submission.compile_files),
            )
        except Exception as exc:
            self._s.batch_scheduler.finish_materialization(
                int(batch_id),
                success=False,
                error_text=f"judgehost materialization failed: {exc}",
                now_text=now_iso(),
            )
            self._result._domjudge_finalize_batch_if_ready(
                int(batch_id),
                force_failed=True,
                error_text=f"judgehost materialization failed: {exc}",
            )
            return False
        return self._s.batch_scheduler.finish_materialization(
            int(batch_id),
            success=True,
            error_text="",
            now_text=now_iso(),
        )


    def _domjudge_apply_cache_shortcuts_for_batch(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
        deadline: float | None,
    ) -> int:
        batch_row = self._s.batch_scheduler.fetch_batch(int(batch_id))
        if batch_row is None:
            return 0
        compile_cfg = self._domjudge_config_object(batch_row["compile_config_json"])
        run_cfg = self._domjudge_config_object(batch_row["run_config_json"])
        compare_cfg = self._domjudge_config_object(batch_row["compare_config_json"])
        compile_config_hash = domjudge_json_hash(compile_cfg)
        run_config_hash = domjudge_json_hash(run_cfg)
        compare_config_hash = domjudge_json_hash(compare_cfg)
        toolchain_cmd_digest = domjudge_text(compile_cfg.get("toolchain_cmd_digest"))
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._toolkit.toolchain_cmd_digest(domjudge_text(batch_row["source_name"]))

        claims = self._s.batch_scheduler.claim_cache_cases(
            int(batch_id),
            hostname=hostname,
            limit=max(0, int(limit)),
            now_text=now_iso(),
        )
        if not claims:
            return 0

        processed = 0
        finished_claims: list[tuple[CaseClaim, CaseResult | None]] = []
        unprocessed: list[CaseClaim] = []
        for claim, row in claims:
            if deadline is not None and processed > 0 and time.monotonic() >= deadline:
                unprocessed.append(claim)
                continue
            try:
                shortcut = self._domjudge_try_cache_shortcut(
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
                    int(batch_id),
                    int(row["id"]),
                )
                shortcut = None
            finished_claims.append((claim, shortcut))
            processed += 1
        outcomes = self._s.batch_scheduler.finish_cache_claims(
            finished_claims,
            updated_at=now_iso(),
        )
        for claim, _shortcut in finished_claims:
            outcome = outcomes.get(claim.case_id)
            try:
                if outcome == "reported":
                    self._result._domjudge_publish_reported_case(
                        task_id=claim.task_id,
                        test_name=claim.test_name,
                    )
                elif outcome == "cancelled":
                    self._result._publish_verification_case_cancelled(
                        task_id=claim.task_id,
                        test_name=claim.test_name,
                    )
            except Exception:
                logger.exception(
                    "failed to publish cached DOMjudge case batch_id=%s case_id=%s",
                    int(batch_id),
                    claim.case_id,
                )
            if outcome in {"reported", "cancelled"}:
                self._result._domjudge_finalize_task_if_ready(
                    claim.task_id,
                    batch_row=dict(batch_row),
                )
        if unprocessed:
            self._s.batch_scheduler.abort_cache_claims(
                unprocessed,
                updated_at=now_iso(),
            )
        self._result._domjudge_finalize_batch_if_ready(batch_id)
        return processed
