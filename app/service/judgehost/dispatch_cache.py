from __future__ import annotations

import logging
import re
from typing import cast, TypedDict

from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_json_hash
from app.service.judgehost.runtime import (
    domjudge_bool,
    domjudge_parse_float,
    domjudge_verdict_from_runresult,
    domjudge_rewrite_untrusted_runresult,
)
from app.service.judgehost.shared import domjudge_lower_text, domjudge_path_name, domjudge_text

from .job_scheduler_models import JudgehostCaseRow, JudgehostJobRow

logger = logging.getLogger(__name__)
_DomjudgeCacheEntry = TypedDict(
    "_DomjudgeCacheEntry",
    {
        "value": dict[str, object],
        "files": dict[str, object],
    },
)

_DomjudgeCachedCaseBundle = TypedDict(
    "_DomjudgeCachedCaseBundle",
    {
        "runresult": str,
        "runtime_sec": float,
        "cpu_sec": float,
        "wall_sec": float,
        "memory_kb": int,
        "output_run_rel": str,
        "output_error_rel": str | None,
        "output_system_rel": str | None,
        "output_diff_rel": str | None,
        "metadata_rel": str | None,
        "compare_metadata_rel": str | None,
        "team_message_rel": str | None,
        "score_text": str | None,
        "feedback_text": str,
        "feedback_files": list[str],
        "answer_correct": bool,
        "test_row": dict[str, object],
    },
)


class DispatchCacheMixin:
    """Resolve result-cache hits and materialize executable Jobs on misses."""

    def _domjudge_try_cache_shortcut(
        self,
        *,
        job_row: JudgehostJobRow,
        case_row: JudgehostCaseRow,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
    ) -> _DomjudgeCachedCaseBundle | None:
        source_hash = domjudge_lower_text(job_row["source_hash"])
        compile_hash = domjudge_lower_text(job_row["compile_hash"])
        run_hash = domjudge_lower_text(job_row["run_hash"])
        compare_hash = domjudge_lower_text(job_row["compare_hash"])
        testcase_hash = domjudge_lower_text(case_row["testcase_hash"])
        testcase_input_hash = domjudge_lower_text(case_row["testcase_input_hash"])
        testcase_answer_hash = domjudge_lower_text(case_row["testcase_answer_hash"])
        if not testcase_hash:
            raise RuntimeError(f"missing testcase_hash for DOMjudge case {int(case_row['id'])}")
        if not testcase_input_hash:
            raise RuntimeError(f"missing testcase_input_hash for DOMjudge case {int(case_row['id'])}")
        if not testcase_answer_hash:
            raise RuntimeError(f"missing testcase_answer_hash for DOMjudge case {int(case_row['id'])}")

        force_recompile = domjudge_bool(job_row["force_recompile"], default=False)
        expected_behavior = domjudge_lower_text(job_row["expected_behavior"], default="unknown")
        verification_source = domjudge_lower_text(job_row["verification_source"])
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
        if force_recompile:
            self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
            return None

        run_cfg_obj = self._domjudge_config_object(job_row["run_config_json"])

        blob_names = [
            "program.err",
            "judgemessage.txt",
            "teammessage.txt",
            "compare.meta",
        ]
        requires_output_blob = main_correct or "generate-input" in verification_source
        if requires_output_blob:
            blob_names.append("program.out")
        cached_read = self._toolkit.cache_get_with_blobs(
            self.CASE_CACHE_KIND,
            case_key_hash,
            case_signature,
            names=blob_names,
        )
        if cached_read is None:
            return None
        cached_entry, cached_blobs = cached_read
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
                cache_kind=self.CASE_CACHE_KIND,
                cache_key_hash=case_key_hash,
                cache_signature=case_signature,
                cache_value=cached_obj,
                cache_files=cached_exact["files"],
            )
            cached_result = self._domjudge_cached_case_bundle(
                test_name=domjudge_text(case_row["test_name"]),
                runresult=cached_runresult,
                built=built,
                blobs=cached_blobs,
                cache_key_hash=case_key_hash,
                cache_signature=case_signature,
            )
            if cached_verdict == "OK" and (not compile_only):
                # Cached OK result must carry a resolvable output artifact.
                if not cached_result["output_run_rel"]:
                    self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                    return None
            if requires_output_blob and "program.out" not in cached_blobs:
                self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            return cached_result

        return None

    def _domjudge_materialize_job(self, job_id: int) -> bool:
        with self._result._job_activity(int(job_id)):
            job_row = self._s.job_scheduler.fetch_job(int(job_id))
            if job_row is None or domjudge_lower_text(job_row["status"]) != "open":
                return False
            state = domjudge_lower_text(job_row["materialization_state"])
            if state == "ready":
                return True
            if state == "failed":
                return False
            now_text = now_iso()
            if not self._s.job_scheduler.claim_materialization(
                int(job_id),
                now_text=now_text,
            ):
                refreshed = self._s.job_scheduler.fetch_job(int(job_id))
                return bool(refreshed is not None and refreshed["materialization_state"] == "ready")
            spec = self._s.job_scheduler.job_spec(int(job_id))
            if spec is None:
                self._s.job_scheduler.finish_materialization(
                    int(job_id),
                    source_path="",
                    work_root="",
                    success=False,
                    now_text=now_iso(),
                )
                self._result._domjudge_finalize_if_ready(
                    int(job_id),
                    force_failed=True,
                    error_text="judgehost job specification disappeared",
                )
                return False
            work_root = self._toolkit.work_root(domjudge_text(job_row["task_id"]))
            source_dir = (work_root / "source").resolve()
            source_path = (source_dir / domjudge_path_name(job_row["source_name"])).resolve()
            try:
                source_dir.mkdir(parents=True, exist_ok=True)
                self._toolkit.ensure_bytes_file(
                    source_path,
                    spec.source_bytes,
                    executable=False,
                )
                for name, blob in spec.extra_source_items:
                    target = (source_dir / name).resolve()
                    if target != source_path:
                        self._toolkit.ensure_bytes_file(target, blob, executable=False)
                for kind, hash_key, files_key in (
                    ("compile", "compile_hash", "compile_files"),
                    ("run", "run_hash", "run_files"),
                    ("compare", "compare_hash", "compare_files"),
                ):
                    self._toolkit.store_executable_cache(
                        kind=kind,
                        executable_hash=domjudge_text(job_row[hash_key]),
                        files=list(getattr(spec, files_key)),
                    )
            except Exception as exc:
                self._s.job_scheduler.finish_materialization(
                    int(job_id),
                    source_path=str(source_path),
                    work_root=str(work_root),
                    success=False,
                    now_text=now_iso(),
                )
                self._result._domjudge_finalize_if_ready(
                    int(job_id),
                    force_failed=True,
                    error_text=f"judgehost materialization failed: {exc}",
                )
                return False
            return self._s.job_scheduler.finish_materialization(
                int(job_id),
                source_path=str(source_path),
                work_root=str(work_root),
                success=True,
                now_text=now_iso(),
            )


    def _domjudge_apply_cache_shortcuts_for_job(self, job_id: int, *, hostname: str) -> int:
        # Serialize only this job's artifact activity; unrelated hosts remain concurrent.
        with self._result._job_activity(int(job_id)):
            return self._domjudge_apply_cache_shortcuts_for_job_locked(
                job_id,
                hostname=hostname,
            )

    def _domjudge_apply_cache_shortcuts_for_job_locked(
        self,
        job_id: int,
        *,
        hostname: str,
    ) -> int:
        job_row = self._s.job_scheduler.fetch_job(int(job_id))
        if job_row is None:
            return 0
        compile_cfg = self._domjudge_config_object(job_row["compile_config_json"])
        run_cfg = self._domjudge_config_object(job_row["run_config_json"])
        compare_cfg = self._domjudge_config_object(job_row["compare_config_json"])
        compile_config_hash = domjudge_json_hash(compile_cfg)
        run_config_hash = domjudge_json_hash(run_cfg)
        compare_config_hash = domjudge_json_hash(compare_cfg)
        toolchain_cmd_digest = domjudge_text(compile_cfg.get("toolchain_cmd_digest"))
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._toolkit.toolchain_cmd_digest(domjudge_text(job_row["source_name"]))

        # Do not remove entries from the cache-pending heap until all shared
        # probe inputs are ready. A setup failure must leave the cases retryable.
        rows = self._s.job_scheduler.cache_pending_cases(int(job_id))
        if not rows:
            return 0

        now_text = now_iso()
        cached_case_updates: list[dict[str, object]] = []
        cache_miss_ids: list[int] = []
        for row in rows:
            try:
                cache_key_hash, cache_signature = self._toolkit.case_cache_ref(
                    source_hash=domjudge_lower_text(job_row["source_hash"]),
                    compile_hash=domjudge_lower_text(job_row["compile_hash"]),
                    run_hash=domjudge_lower_text(job_row["run_hash"]),
                    compare_hash=domjudge_lower_text(job_row["compare_hash"]),
                    compile_config_hash=compile_config_hash,
                    run_config_hash=run_config_hash,
                    compare_config_hash=compare_config_hash,
                    toolchain_cmd_digest=toolchain_cmd_digest,
                    testcase_hash=domjudge_lower_text(row["testcase_hash"]),
                )
                with self._toolkit.cache_key_lock(
                    self.CASE_CACHE_KIND,
                    cache_key_hash,
                    cache_signature,
                ):
                    shortcut = self._domjudge_try_cache_shortcut(
                        job_row=job_row,
                        case_row=row,
                        compile_config_hash=compile_config_hash,
                        run_config_hash=run_config_hash,
                        compare_config_hash=compare_config_hash,
                        toolchain_cmd_digest=toolchain_cmd_digest,
                    )
            except Exception:
                logger.exception(
                    "judgehost result-cache probe failed job_id=%s case_id=%s",
                    int(job_id),
                    int(row["id"]),
                )
                shortcut = None
            if shortcut is None:
                cache_miss_ids.append(int(row["id"]))
                continue
            cached_case_updates.append(
                {
                    "case_id": int(row["id"]),
                    "runresult": shortcut["runresult"],
                    "runtime_sec": shortcut["runtime_sec"],
                    "cpu_sec": shortcut["cpu_sec"],
                    "wall_sec": shortcut["wall_sec"],
                    "memory_kb": shortcut["memory_kb"],
                    "output_run_rel": shortcut["output_run_rel"],
                    "output_error_rel": shortcut["output_error_rel"],
                    "output_system_rel": shortcut["output_system_rel"],
                    "output_diff_rel": shortcut["output_diff_rel"],
                    "metadata_rel": shortcut["metadata_rel"],
                    "compare_metadata_rel": shortcut["compare_metadata_rel"],
                    "team_message_rel": shortcut["team_message_rel"],
                    "score_text": shortcut["score_text"],
                    "task_id": row["task_id"],
                    "test_name": row["test_name"],
                    "test_row": shortcut["test_row"],
                }
            )

        if cache_miss_ids:
            self._s.job_scheduler.mark_cache_misses(
                cache_miss_ids,
                now_text=now_text,
            )
        if not cached_case_updates:
            return len(cache_miss_ids)

        applied_case_ids = self._s.job_scheduler.apply_cached_case_results(
            cached_rows=cached_case_updates,
            lease_owner=hostname,
            now_text=now_text,
        )
        applied_id_set = set(applied_case_ids)
        applied_by_task: dict[str, list[dict[str, object]]] = {}
        for cached in cached_case_updates:
            if int(cached["case_id"]) not in applied_id_set:
                continue
            task_id = domjudge_text(cached["task_id"])
            applied_by_task.setdefault(task_id, []).append(cached)

        published_case_ids: list[int] = []
        prepared_rows_by_task: dict[str, dict[str, dict[str, object]]] = {}
        for task_id, task_cached_rows in applied_by_task.items():
            test_rows = [
                dict(cast(dict[str, object], cached["test_row"]))
                for cached in task_cached_rows
            ]
            self._result._domjudge_update_verification_run_case_progress_batch(
                task_id=task_id,
                source_path=domjudge_text(job_row["source_path"]),
                test_rows=test_rows,
                run_status="running",
            )
            task_row = self._core.task_by_id(task_id)
            if task_row is None:
                continue
            prepared_rows_by_task[task_id] = {
                domjudge_text(test_row["test"]): test_row
                for test_row in test_rows
            }
            for cached, test_row in zip(task_cached_rows, test_rows, strict=True):
                try:
                    case_result = self._result._domjudge_case_result_from_test_row(
                        task_id=task_id,
                        task_row=task_row,
                        test_row=test_row,
                    )
                    if self._result._publish_verification_case_result(
                        task_id=task_id,
                        test_name=domjudge_text(cached["test_name"]),
                        case_result=case_result,
                        verification_id=domjudge_text(task_row["verification_id"]),
                    ):
                        published_case_ids.append(int(cached["case_id"]))
                except Exception:
                    logger.exception(
                        "failed to publish cached DOMjudge case job_id=%s case_id=%s",
                        int(job_id),
                        int(cached["case_id"]),
                    )
        if published_case_ids:
            self._s.job_scheduler.mark_cases_verification_published(published_case_ids)

        for task_id in applied_by_task:
            try:
                self._result._domjudge_finalize_task_if_ready(
                    task_id,
                    job_row=dict(job_row),
                    prepared_test_rows=prepared_rows_by_task.get(task_id),
                )
            except Exception:
                logger.exception(
                    "failed to finalize cached DOMjudge task task_id=%s job_id=%s",
                    task_id,
                    int(job_id),
                )
        self._result._domjudge_finalize_if_ready(job_id)
        return len(cache_miss_ids)
