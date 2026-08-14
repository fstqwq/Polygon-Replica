import logging
import re
import time
from typing import TypedDict

from app.db import now_iso
from app.service.judgehost.domjudge.result import (
    parse_bool,
    parse_nonnegative_float,
    verdict_from_runresult,
    rewrite_untrusted_runresult,
)
from app.service.judgehost.domjudge.codec import decode_text

from app.service.judgehost.batch.model import (
    CaseClaim,
    CaseResult,
    CompileSubmission,
    ExecutionBatchRow,
    JudgehostCaseRow,
)
from app.service.execution.codec import execution_result_from_json
from app.service.platform.runtime_cache_index import RuntimeCacheIndex

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

    def _try_cache_shortcut(
        self,
        *,
        batch_row: ExecutionBatchRow,
        case_row: JudgehostCaseRow,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
    ) -> CaseResult | None:
        source_hash = batch_row["source_hash"]
        compile_hash = batch_row["compile_hash"]
        run_hash = batch_row["run_hash"]
        compare_hash = batch_row["compare_hash"]
        testcase_hash = case_row["testcase_hash"]
        testcase_input_hash = case_row["testcase_input_hash"]
        testcase_answer_hash = case_row["testcase_answer_hash"]
        if not testcase_hash:
            raise RuntimeError(f"missing testcase_hash for DOMjudge case {int(case_row['id'])}")
        if not testcase_input_hash:
            raise RuntimeError(
                f"missing testcase_input_hash for DOMjudge case {int(case_row['id'])}"
            )
        if not testcase_answer_hash:
            raise RuntimeError(
                f"missing testcase_answer_hash for DOMjudge case {int(case_row['id'])}"
            )

        bypass_case_result_cache = parse_bool(batch_row["bypass_case_result_cache"], default=False)
        expected_behavior = batch_row["expected_behavior"] or "unknown"
        verification_source = batch_row["verification_source"]
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

        run_cfg_obj = self._config_object(batch_row["run_config_json"])

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
        cached_entry, _cached_payloads = cached_read
        cached_exact = self._cache_entry(cached_entry)
        if cached_exact is not None:
            cached_obj = cached_exact["value"]
            if not parse_bool(cached_obj.get("shortcut_eligible"), default=True):
                return None
            cached_runresult = decode_text(raw=cached_obj.get("runresult"))
            cached_runresult = rewrite_untrusted_runresult(
                cached_runresult,
                cpu_sec=parse_nonnegative_float(
                    cached_obj.get("cpu_sec"),
                    parse_nonnegative_float(cached_obj.get("runtime_sec"), 0.0),
                ),
                run_cfg_obj=run_cfg_obj,
            )
            cached_verdict = verdict_from_runresult(cached_runresult)
            if cached_verdict == "FL":
                self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            # Build answer generation, expected accepted runs, and compile-only tasks
            # must not reuse non-OK cached outcomes; otherwise transient failures can
            # poison later requests.
            if (
                main_correct or expected_behavior in {"accepted", "compile"}
            ) and cached_verdict != "OK":
                if expected_behavior == "compile":
                    self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            result_json = decode_text(raw=cached_obj.get("result_json"))
            if not result_json:
                self._toolkit.cache_delete(
                    self.CASE_CACHE_KIND,
                    case_key_hash,
                    case_signature,
                )
                return None
            cached_result = execution_result_from_json(result_json)
            if any(
                self._s.runtime_blob_store.descriptor(token) is None
                for token in cached_result.artifact_refs()
            ):
                self._toolkit.cache_delete(
                    self.CASE_CACHE_KIND,
                    case_key_hash,
                    case_signature,
                )
                return None
            if cached_verdict == "OK" and (not compile_only):
                # Cached OK result must carry a resolvable output artifact.
                if not cached_result.output_run_ref:
                    self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                    return None
            if requires_output_blob and not cached_result.output_run_ref:
                self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            return cached_result

        return None

    def _materialize_batch(self, batch_id: int) -> bool:
        batch_row = self._s.batch_runtime.fetch_batch(int(batch_id))
        if batch_row is None or batch_row["status"] != "open":
            return False
        state = batch_row["materialization_state"]
        if state == "ready":
            return True
        if state == "failed":
            return False
        if not self._s.batch_runtime.claim_materialization(int(batch_id), now_text=now_iso()):
            refreshed = self._s.batch_runtime.fetch_batch(int(batch_id))
            return bool(refreshed is not None and refreshed["materialization_state"] == "ready")
        spec = self._s.batch_runtime.batch_spec(int(batch_id))
        submission = self._s.batch_runtime.compile_submission_for_batch(int(batch_id))
        if spec is None or submission is None:
            self._s.batch_runtime.finish_materialization(
                int(batch_id),
                success=False,
                error_text="judgehost batch specification disappeared",
                now_text=now_iso(),
            )
            self._batch_finalizer.finalize_batch_if_ready(
                int(batch_id),
                force_failed=True,
                error_text="judgehost batch specification disappeared",
                require_completion_ack=True,
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
            self._s.batch_runtime.publish_materialized_compile_submission(
                submission.compile_key,
                materialized_submission,
            )
            for kind, hash_key, files_key in (
                ("run", "run_hash", "run_files"),
                ("compare", "compare_hash", "compare_files"),
            ):
                self._toolkit.store_executable_cache(
                    kind=kind,
                    executable_hash=batch_row[hash_key],
                    files=list(getattr(spec, files_key)),
                )
            self._toolkit.store_executable_cache(
                kind="compile",
                executable_hash=batch_row["compile_hash"],
                files=list(submission.compile_files),
            )
        except Exception as exc:
            self._s.batch_runtime.finish_materialization(
                int(batch_id),
                success=False,
                error_text=f"judgehost materialization failed: {exc}",
                now_text=now_iso(),
            )
            self._batch_finalizer.finalize_batch_if_ready(
                int(batch_id),
                force_failed=True,
                error_text=f"judgehost materialization failed: {exc}",
                require_completion_ack=True,
            )
            return False
        return self._s.batch_runtime.finish_materialization(
            int(batch_id),
            success=True,
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
    ) -> int:
        batch_row = self._s.batch_runtime.fetch_batch(int(batch_id))
        if batch_row is None:
            return 0
        compile_cfg = self._config_object(batch_row["compile_config_json"])
        run_cfg = self._config_object(batch_row["run_config_json"])
        compare_cfg = self._config_object(batch_row["compare_config_json"])
        compile_config_hash = RuntimeCacheIndex.signature(compile_cfg)
        run_config_hash = RuntimeCacheIndex.signature(run_cfg)
        compare_config_hash = RuntimeCacheIndex.signature(compare_cfg)
        toolchain_cmd_digest = decode_text(raw=compile_cfg.get("toolchain_cmd_digest"))
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._toolkit.toolchain_cmd_digest(batch_row["source_name"])

        claims = self._s.batch_runtime.claim_cache_cases(
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
                    int(batch_id),
                    int(row["id"]),
                )
                shortcut = None
            finished_claims.append((claim, shortcut))
            processed += 1
        self._s.batch_runtime.finish_cache_claims(
            finished_claims,
            updated_at=now_iso(),
        )
        if unprocessed:
            self._s.batch_runtime.abort_cache_claims(
                unprocessed,
                updated_at=now_iso(),
            )
        self._batch_finalizer.finalize_batch_if_ready(
            batch_id,
            require_completion_ack=True,
        )
        return processed
