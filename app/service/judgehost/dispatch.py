from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import cast
from typing import TypedDict

from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_json_hash
from app.service.judgehost.domjudge.client import domjudge_script_id
from app.service.memory.judgehost_state_store import JudgehostCaseRow, JudgehostJobRow
from app.service.judgehost.shared import domjudge_lower_text, domjudge_path_name, domjudge_text
from app.service.judgehost.runtime import (
    domjudge_bool,
    domjudge_parse_float,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_rewrite_untrusted_runresult,
    domjudge_verdict_from_runresult,
)
from app.service.platform.hashing import sha256_hex_bytes as domjudge_sha256_bytes
from app.service.run.runtime import RUN_TEST_NAME_RE
from app.service.verification.task_scheduler import notify_verification_case_leased

from .core import JudgehostCore
from .result import ResultProcessor
from .state import JudgehostState
from .task_queue import TaskQueue
from .toolkit import DomjudgeToolkit

logger = logging.getLogger(__name__)

_NUMERIC_TEST_NAME_RE = re.compile(r"^([0-9]+)\.in$")


_DomjudgePreparedTestRow = TypedDict(
    "_DomjudgePreparedTestRow",
    {
        "name": str,
        "input_b64": object,
        "answer_b64": object,
    },
)

_DomjudgePrecomputedBundle = TypedDict(
    "_DomjudgePrecomputedBundle",
    {
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
        "source_bytes": bytes,
        "extra_source_items": list[tuple[str, bytes]],
        "tests_rows": list[_DomjudgePreparedTestRow],
        "run_cfg_obj": dict[str, object],
        "problem_limits_obj": dict[str, object],
        "checker_args": list[str],
        "contest_id": str,
        "mode": str,
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
        "force_recompile": bool,
        "manual_validate_only": bool,
        "checker_bytes": bytes,
        "validator_bytes": bytes,
        "interactor_bytes": bytes,
        "checker_source_bytes": bytes,
        "validator_source_bytes": bytes,
        "interactor_source_bytes": bytes,
        "testlib_header_bytes": bytes,
    },
)

_DomjudgeCacheEntry = TypedDict(
    "_DomjudgeCacheEntry",
    {
        "value": dict[str, object],
        "files": dict[str, object],
    },
)

_DomjudgeCachedCaseResult = TypedDict(
    "_DomjudgeCachedCaseResult",
    {
        "lease_owner": str,
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
    },
)


class DispatchHandler:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_ENQUEUING = "enqueuing"
    CASE_CACHE_KIND = DomjudgeToolkit.CASE_CACHE_KIND
    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _TASK_KIND_MAIN_CORRECT = "main-correct"

    def __init__(self, state: JudgehostState, core: JudgehostCore, queue: TaskQueue, result: ResultProcessor, toolkit: DomjudgeToolkit) -> None:
        self._s = state
        self._core = core
        self._queue = queue
        self._result = result
        self._toolkit = toolkit

    @staticmethod
    def _domjudge_visible_testcase_id(*, test_name: str, ordinal: int) -> int:
        token = domjudge_text(test_name)
        match = _NUMERIC_TEST_NAME_RE.fullmatch(token)
        if match is not None:
            return max(1, int(match.group(1)))
        return max(1, int(ordinal))

    def _domjudge_case_rows(
        self,
        *,
        task_id: str,
        run_id: str,
        tests_rows: list[_DomjudgePreparedTestRow],
        main_correct: bool,
    ) -> list[dict[str, object]]:
        case_rows: list[dict[str, object]] = []
        ordinal = 0
        for entry in tests_rows:
            ordinal += 1
            raw_name = domjudge_text(entry.get("name"))
            test_name = raw_name if RUN_TEST_NAME_RE.fullmatch(raw_name) else f"{ordinal:03}.in"
            in_bytes = self._toolkit.b64_decode(entry.get("input_b64"))
            ans_bytes = self._toolkit.b64_decode(entry.get("answer_b64"))
            testcase_input_hash = domjudge_sha256_bytes(in_bytes)
            testcase_answer_hash = domjudge_sha256_bytes(ans_bytes)
            testcase_hash = (
                testcase_input_hash
                if main_correct
                else self._toolkit.set_hash_from_blobs([in_bytes, ans_bytes])
            )
            input_ref_text, answer_ref_text = self._toolkit.register_cached_testcase(
                testcase_hash=testcase_hash,
                in_bytes=in_bytes,
                ans_bytes=ans_bytes,
            )
            testcase_id = self._domjudge_visible_testcase_id(test_name=test_name, ordinal=ordinal)
            case_rows.append(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "test_name": test_name,
                    "ordinal": ordinal,
                    "testcase_id": testcase_id,
                    "testcase_hash": testcase_hash,
                    "testcase_input_hash": testcase_input_hash,
                    "testcase_answer_hash": testcase_answer_hash,
                    "input_ref": input_ref_text,
                    "answer_ref": answer_ref_text,
                    "status": "pending",
                }
            )
        return case_rows

    def domjudge_register_host(self, hostname: str) -> list[dict[str, object]]:
        safe_host = self._core.normalize_hostname(hostname)
        self._queue._requeue_expired_leases(force=True)
        self._queue._record_host_event_conn(hostname=safe_host, action="register")
        # judgedaemon calls /judgehosts periodically as a heartbeat. Treating
        # re-registration as a hard reconnect and requeueing unfinished work
        # here causes duplicate delivery of leased cases, which can wedge the
        # daemon in a restart loop inside the same work_root. Real recovery
        # from dead hosts is already handled by lease expiry and explicit host
        # disable paths, so keep register_host idempotent.
        return []

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
        source_bytes = self._toolkit.b64_decode(payload.get("source_b64"))
        if not source_bytes:
            raise RuntimeError("submission source payload is empty")

        extra_sources_payload = cast(dict[str, object] | None, payload.get("extra_sources_b64"))
        if extra_sources_payload is None:
            extra_sources_payload = {}
        extra_source_items: list[tuple[str, bytes]] = []
        for raw_name, raw_blob in sorted(extra_sources_payload.items()):
            safe_name = domjudge_path_name(raw_name)
            if (not safe_name) or safe_name == source_name:
                continue
            blob = self._toolkit.b64_decode(raw_blob)
            if not blob:
                continue
            extra_source_items.append((safe_name, blob))

        verification_payload = cast(dict[str, object] | None, payload.get("verification_payload"))
        if verification_payload is None:
            raise RuntimeError("verification payload is required for DOMjudge compatibility")

        tests_payload = cast(list[_DomjudgePreparedTestRow] | None, verification_payload.get("tests"))
        tests_rows = [] if tests_payload is None else list(tests_payload)
        if compile_only:
            tests_rows = [
                {
                    "name": "compile-only.in",
                    "input_b64": "",
                    "answer_name": "compile-only.ans",
                    "answer_b64": "",
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

        binaries_payload = cast(dict[str, object] | None, verification_payload.get("binaries_b64"))
        if binaries_payload is None:
            binaries_payload = {}
        sources_payload = cast(dict[str, object] | None, verification_payload.get("sources_b64"))
        if sources_payload is None:
            sources_payload = {}
        precomputed = self._domjudge_precomputed_bundle(payload.get("domjudge_precomputed"))
        if precomputed is None:
            raise RuntimeError("domjudge precomputed payload is required")

        return {
            "source_name": source_name,
            "source_bytes": source_bytes,
            "extra_source_items": extra_source_items,
            "tests_rows": tests_rows,
            "run_cfg_obj": run_cfg_obj,
            "problem_limits_obj": problem_limits_obj,
            "checker_args": checker_args,
            "contest_id": "local",
            "mode": domjudge_lower_text(payload.get("mode"), default="pass-fail"),
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
            "force_recompile": domjudge_bool(payload.get("force_recompile"), default=False),
            "manual_validate_only": domjudge_bool(payload.get("manual_validate_only"), default=False),
            "checker_bytes": self._toolkit.b64_decode(binaries_payload.get("checker")),
            "validator_bytes": self._toolkit.b64_decode(binaries_payload.get("validator")),
            "interactor_bytes": self._toolkit.b64_decode(binaries_payload.get("interactor")),
            "checker_source_bytes": self._toolkit.b64_decode(sources_payload.get("checker.cpp")),
            "validator_source_bytes": self._toolkit.b64_decode(sources_payload.get("validator.cpp")),
            "interactor_source_bytes": self._toolkit.b64_decode(sources_payload.get("interactor.cpp")),
            "testlib_header_bytes": self._toolkit.b64_decode(sources_payload.get("testlib.h")),
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

    def _domjudge_cached_case_result(
        self,
        *,
        hostname: str,
        runresult: str,
        built: dict[str, object],
    ) -> _DomjudgeCachedCaseResult:
        output_run_rel = domjudge_text(built.get("output_run_rel"))
        return {
            "lease_owner": hostname,
            "runresult": runresult,
            "runtime_sec": domjudge_parse_float(built.get("runtime_sec"), 0.0),
            "cpu_sec": domjudge_parse_float(built.get("cpu_sec"), 0.0),
            "wall_sec": domjudge_parse_float(built.get("wall_sec"), 0.0),
            "memory_kb": domjudge_parse_int(built.get("memory_kb"), 0),
            "output_run_rel": output_run_rel,
            "output_error_rel": domjudge_text(built.get("output_error_rel")) or None,
            "output_system_rel": domjudge_text(built.get("output_system_rel")) or None,
            "output_diff_rel": domjudge_text(built.get("output_diff_rel")) or None,
            "metadata_rel": domjudge_text(built.get("metadata_rel")) or None,
            "compare_metadata_rel": domjudge_text(built.get("compare_metadata_rel")) or None,
            "team_message_rel": domjudge_text(built.get("team_message_rel")) or None,
            "score_text": domjudge_text(built.get("score_text")) or None,
        }


    def _domjudge_prepare_job(self, hostname: str, task: dict[str, object], *, group_key: str = "") -> int:
        safe_host = self._core.normalize_hostname(hostname)
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
        source_bytes = prepared["source_bytes"]
        extra_source_items = prepared["extra_source_items"]
        contest_id = prepared["contest_id"]
        mode = prepared["mode"]
        compile_hash = prepared["compile_hash"]
        run_hash = prepared["run_hash"]
        compare_hash = prepared["compare_hash"]
        source_hash = prepared["source_hash"]
        compile_config = prepared["compile_config"]
        run_config = prepared["run_config"]
        compare_config = prepared["compare_config"]
        tests_rows = prepared["tests_rows"]
        expected_behavior = prepared["expected_behavior"]
        verification_source = prepared["verification_source"]
        force_recompile = prepared["force_recompile"]
        compile_files = prepared["compile_files"]
        run_files = prepared["run_files"]
        compare_files = prepared["compare_files"]
        main_correct = prepared["main_correct"]

        work_root = self._toolkit.work_root(task_id)
        source_dir = (work_root / "source").resolve()
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path = (source_dir / source_name).resolve()
        self._toolkit.ensure_bytes_file(source_path, source_bytes, executable=False)
        for name, blob in extra_source_items:
            target = (source_dir / name).resolve()
            if target == source_path:
                continue
            self._toolkit.ensure_bytes_file(target, blob, executable=False)
        self._toolkit.store_executable_cache(kind="compile", executable_hash=compile_hash, files=compile_files)
        self._toolkit.store_executable_cache(kind="run", executable_hash=run_hash, files=run_files)
        self._toolkit.store_executable_cache(kind="compare", executable_hash=compare_hash, files=compare_files)

        now_text = now_iso()
        case_rows = self._domjudge_case_rows(
            task_id=task_id,
            run_id=run_id,
            tests_rows=tests_rows,
            main_correct=main_correct,
        )

        job_id = self._s.judgehost_state_store.create_job_with_cases(
            task_id=task_id,
            run_id=run_id,
            group_key=group_key,
            submit_id=str(int(time.time() * 1000)),
            contest_id=contest_id,
            mode=mode,
            source_name=source_name,
            source_path=str(source_path),
            work_root=str(work_root),
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            source_hash=source_hash,
            compile_config_json=json.dumps(compile_config, ensure_ascii=False, separators=(",", ":")),
            run_config_json=json.dumps(run_config, ensure_ascii=False, separators=(",", ":")),
            compare_config_json=json.dumps(compare_config, ensure_ascii=False, separators=(",", ":")),
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            force_recompile=1 if force_recompile else 0,
            lease_owner=safe_host,
            status="leased",
            created_at=now_text,
            case_rows=case_rows,
        )
        self._queue.compact_task_payload(task_id)
        return job_id

    def _domjudge_append_task_to_job(self, job_id: int, task: dict[str, object]) -> str:
        task_id, run_id, payload = self._domjudge_task_payload(task)
        latest_task_row = self._core.task_by_id(task_id)
        if latest_task_row is not None:
            payload = dict(latest_task_row.get("payload") or {})
            run_id = domjudge_text(latest_task_row.get("run_id"), default=run_id)
        prepared = self._domjudge_prepare_payload(
            payload,
            compile_only=self._toolkit.task_kind(payload) == self._TASK_KIND_COMPILE_ONLY,
        )
        case_rows = self._domjudge_case_rows(
            task_id=task_id,
            run_id=run_id,
            tests_rows=prepared["tests_rows"],
            main_correct=prepared["main_correct"],
        )
        append_result = self._s.judgehost_state_store.append_cases_to_job(
            job_id=int(job_id),
            case_rows=case_rows,
            now_text=now_iso(),
        )
        outcome = str(append_result["outcome"])
        if outcome == "closed":
            return outcome
        if int(append_result.get("inserted") or 0) > 0:
            self._queue.compact_task_payload(task_id)
            return outcome
        missing_names = [
            str(case_row.get("test_name") or "")
            for case_row in case_rows
            if self._s.judgehost_state_store.case_for_task(task_id, str(case_row.get("test_name") or "")) is None
        ]
        if missing_names:
            raise RuntimeError(f"grouped DOMjudge job append failed for {', '.join(missing_names)}")
        self._queue.compact_task_payload(task_id)
        return outcome

    def _domjudge_absorb_grouped_tasks(self, *, job_id: int, hostname: str, group_key: str) -> int:
        active_job_id = int(job_id)
        while True:
            leased_task = self._queue._lease_matching_group_task(hostname=hostname, group_key=group_key)
            if leased_task is None:
                break
            outcome = self._domjudge_append_task_to_job(active_job_id, leased_task)
            if outcome == "closed":
                active_job_id = self._domjudge_prepare_job(
                    hostname,
                    leased_task,
                    group_key=group_key,
                )
        return active_job_id

    def _domjudge_try_cache_shortcut(
        self,
        *,
        hostname: str,
        job_row: JudgehostJobRow,
        case_row: JudgehostCaseRow,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
    ) -> _DomjudgeCachedCaseResult | None:
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

        cached_exact = self._domjudge_cache_entry(
            self._toolkit.cache_get(self.CASE_CACHE_KIND, case_key_hash, case_signature)
        )
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
            cached_result = self._domjudge_cached_case_result(
                hostname=hostname,
                runresult=cached_runresult,
                built=built,
            )
            if cached_verdict == "OK" and (not compile_only):
                # Cached OK result must carry a resolvable output artifact.
                if not cached_result["output_run_rel"]:
                    self._toolkit.cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                    return None
            return cached_result

        return None


    def _domjudge_release_prepared_job_for_queue(self, job_id: int) -> None:
        now_text = now_iso()
        prequeue_host = self._core.normalize_hostname("prequeue-cache")
        self._s.judgehost_state_store.release_prepared_job_for_queue(
            int(job_id),
            lease_owner=prequeue_host,
            now_text=now_text,
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
        job_row = self._s.judgehost_state_store.fetch_job(int(job_id))
        if job_row is None:
            return 0
        rows = self._s.judgehost_state_store.cases_for_job(int(job_id), status="pending")
        if not rows:
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

        now_text = now_iso()
        work_root = Path(domjudge_text(job_row["work_root"])).resolve()
        cached_case_updates: list[dict[str, object]] = []
        pending_rows = 0
        for row in rows:
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
                    hostname=hostname,
                    job_row=job_row,
                    case_row=row,
                    compile_config_hash=compile_config_hash,
                    run_config_hash=run_config_hash,
                    compare_config_hash=compare_config_hash,
                    toolchain_cmd_digest=toolchain_cmd_digest,
                )
            if shortcut is None:
                pending_rows += 1
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
                }
            )

        if not cached_case_updates:
            return pending_rows

        self._s.judgehost_state_store.apply_cached_case_results(
            cached_rows=cached_case_updates,
            lease_owner=hostname,
            now_text=now_text,
        )
        for cached in cached_case_updates:
            feedback_text, feedback_files = self._result._domjudge_feedback_text_and_files(
                work_root=work_root,
                runresult=domjudge_text(cached["runresult"]),
                output_error_rel=domjudge_text(cached["output_error_rel"]),
                output_diff_rel=domjudge_text(cached["output_diff_rel"]),
                team_message_rel=domjudge_text(cached["team_message_rel"]),
            )
            compare_meta_blob = self._toolkit.read_artifact_blob(work_root, domjudge_text(cached["compare_metadata_rel"]))
            compare_exit_code = -1
            if compare_meta_blob:
                compare_meta = domjudge_parse_meta_text(compare_meta_blob.decode("utf-8", errors="replace"))
                compare_exit_code = domjudge_parse_int(compare_meta.get("exitcode"), -1)
            self._result._domjudge_update_verification_run_case_progress(
                task_id=domjudge_text(cached["task_id"]),
                source_path=domjudge_text(job_row["source_path"]),
                test_name=domjudge_text(cached["test_name"]),
                verdict=domjudge_verdict_from_runresult(domjudge_text(cached["runresult"])),
                runtime_sec=domjudge_parse_float(cached["runtime_sec"], 0.0),
                cpu_sec=domjudge_parse_float(cached["cpu_sec"], 0.0),
                wall_sec=domjudge_parse_float(cached["wall_sec"], 0.0),
                memory_kb=max(0, domjudge_parse_int(cached["memory_kb"], 0)),
                feedback_text=feedback_text,
                output_ref=domjudge_text(cached["output_run_rel"]),
                run_status="running",
                runresult=domjudge_text(cached["runresult"]),
                feedback_files=feedback_files,
                answer_correct=compare_exit_code == 42,
            )
            try:
                self._result._domjudge_publish_reported_case(
                    task_id=domjudge_text(cached["task_id"]),
                    test_name=domjudge_text(cached["test_name"]),
                )
            except Exception:
                logger.exception(
                    "failed to publish cached DOMjudge case job_id=%s case_id=%s",
                    int(job_id),
                    int(cached["case_id"]),
                )
        self._result._domjudge_finalize_if_ready(job_id)
        return pending_rows


    def _domjudge_try_prequeue_cache_finalize(self, *, task_id: str, run_id: str, payload: dict[str, object]) -> None:
        self._domjudge_try_prequeue_cache_finalize_locked(
            task_id=task_id,
            run_id=run_id,
            payload=payload,
        )

    def _domjudge_try_prequeue_cache_finalize_locked(
        self,
        *,
        task_id: str,
        run_id: str,
        payload: dict[str, object],
    ) -> None:
        safe_task_id = domjudge_text(task_id)
        if not safe_task_id:
            return
        compile_only = self._toolkit.task_kind(payload) == self._TASK_KIND_COMPILE_ONLY
        try:
            self._domjudge_prepare_payload(payload, compile_only=compile_only)
        except RuntimeError:
            return

        prequeue_host = self._core.normalize_hostname("prequeue-cache")
        job_id = 0
        try:
            safe_run_id = domjudge_text(run_id)
            task_row = {
                "task_id": safe_task_id,
                "run_id": safe_run_id,
                "payload": payload,
            }
            existing_job = self._s.judgehost_state_store.job_for_run(safe_run_id) if safe_run_id else None
            if existing_job is not None:
                job_id = int(existing_job["job_id"])
                existing_task_id = domjudge_text(existing_job.get("task_id"))
                if safe_task_id != existing_task_id:
                    raise RuntimeError("judgehost run id already belongs to another task")
            else:
                job_id = int(self._domjudge_prepare_job(prequeue_host, task_row))
            job_row = self._s.judgehost_state_store.fetch_job(int(job_id))
            if job_row is None:
                return
            pending_rows = self._domjudge_apply_cache_shortcuts_for_job(int(job_id), hostname=prequeue_host)
            if pending_rows > 0:
                self._domjudge_release_prepared_job_for_queue(int(job_id))
                return

            self._s.task_store.transition(
                safe_task_id,
                expected={self.STATUS_ENQUEUING},
                status=self.STATUS_QUEUED,
                updates={"updated_at": now_iso()},
            )
            self._result._domjudge_finalize_if_ready(int(job_id))
        except Exception as exc:
            if job_id > 0:
                try:
                    self._domjudge_release_prepared_job_for_queue(int(job_id))
                except Exception:
                    pass
            logger.warning("prequeue cache consumption failed task_id=%s: %s", safe_task_id, exc)


    def _domjudge_lease_cases(self, job_id: int, hostname: str, max_batchsize: int) -> list[dict[str, object]]:
        now_text = now_iso()
        job_row = self._s.judgehost_state_store.fetch_job(int(job_id))
        if job_row is None:
            return []
        grouped_job = bool(domjudge_text(job_row["group_key"]))
        cap = max(1, min(256, int(max_batchsize)))
        safe_task_id = domjudge_text(job_row["task_id"])
        if not grouped_job:
            task_row = self._core.task_by_id(safe_task_id)
            if task_row is None:
                job_status = domjudge_lower_text(job_row["status"])
                job_lease_owner = domjudge_text(job_row["lease_owner"])
                if (not safe_task_id) or job_status not in {"queued", "leased"} or job_lease_owner not in {
                    "",
                    hostname,
                    self._core.normalize_hostname("prequeue-cache"),
                }:
                    cancelled_job_ids = self._s.judgehost_state_store.cancel_jobs_for_runs(
                        run_ids=[domjudge_text(job_row["run_id"])],
                        now_text=now_text,
                    )
                    for cancelled_job_id in cancelled_job_ids:
                        self._result._domjudge_finalize_if_ready(cancelled_job_id)
                    return []
            elif task_row.get("status") not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                cancelled_job_ids = self._s.judgehost_state_store.cancel_jobs_for_runs(
                    run_ids=[domjudge_text(job_row["run_id"])],
                    now_text=now_text,
                )
                for cancelled_job_id in cancelled_job_ids:
                    self._result._domjudge_finalize_if_ready(cancelled_job_id)
                return []
        rows = self._s.judgehost_state_store.lease_cases(
            int(job_id),
            hostname=hostname,
            limit=int(cap),
            now_text=now_text,
        )
        if not rows:
            return []
        compile_hash = domjudge_lower_text(job_row["compile_hash"])
        run_hash = domjudge_lower_text(job_row["run_hash"])
        compare_hash = domjudge_lower_text(job_row["compare_hash"])
        compile_id = int(domjudge_script_id(compile_hash))
        run_id_num = int(domjudge_script_id(run_hash))
        compare_id = int(domjudge_script_id(compare_hash))
        safe_submit_id = str(int(job_row["submit_id"]))
        contest_id = domjudge_text(job_row["contest_id"]) or "local"
        compile_config_json = domjudge_text(job_row["compile_config_json"])
        run_config_json = domjudge_text(job_row["run_config_json"])
        compare_config_json = domjudge_text(job_row["compare_config_json"])
        out: list[dict[str, object]] = []
        for row in rows:
            case_id = int(row["id"])
            case_task_id = domjudge_text(row["task_id"], default=safe_task_id)
            out.append(
                {
                    "type": "judging_run",
                    "judgetaskid": case_id,
                    "jobid": int(job_id),
                    "uuid": safe_task_id if grouped_job else case_task_id,
                    "submitid": safe_submit_id,
                    "contestid": contest_id,
                    "compile_script_id": str(int(compile_id)),
                    "run_script_id": str(int(run_id_num)),
                    "compare_script_id": str(int(compare_id)),
                    # DOMjudge fetches testcase files back by this id without
                    # including the judgehost hostname. Use our unique case id
                    # instead of the public test number to avoid collisions
                    # across concurrent jobs and NAT/proxy-shared judgehosts.
                    "testcase_id": str(case_id),
                    "testcase_hash": domjudge_text(row["testcase_hash"]),
                    "compile_config": compile_config_json,
                    "run_config": run_config_json,
                    "compare_config": compare_config_json,
                }
            )
        if grouped_job:
            seen_task_ids: set[str] = set()
            for row in rows:
                case_task_id = domjudge_text(row["task_id"])
                if case_task_id and case_task_id not in seen_task_ids:
                    self._queue.renew_domjudge_task_lease(case_task_id)
                    seen_task_ids.add(case_task_id)
                case_task_row = self._core.task_by_id(case_task_id)
                verification_id = "" if case_task_row is None else domjudge_text(case_task_row.get("verification_id"))
                if verification_id:
                    notify_verification_case_leased(
                        verification_id,
                        case_task_id,
                        domjudge_text(row["test_name"]),
                    )
        else:
            self._queue.renew_domjudge_task_lease(safe_task_id)
            task_row = self._core.task_by_id(safe_task_id)
            verification_id = "" if task_row is None else domjudge_text(task_row.get("verification_id"))
            if verification_id:
                for row in rows:
                    notify_verification_case_leased(
                        verification_id,
                        safe_task_id,
                        domjudge_text(row["test_name"]),
                    )
        return out

    def domjudge_fetch_work(self, hostname: str, max_batchsize: int | None = None) -> list[dict[str, object]]:
        safe_host = self._core.normalize_hostname(hostname)
        if not self._queue._host_enabled_conn(hostname=safe_host):
            self._queue._record_host_event_conn(hostname=safe_host, action="disabled")
            return []
        for finalizing_job_id in self._s.judgehost_state_store.finalizing_job_ids():
            self._result._domjudge_finalize_if_ready(finalizing_job_id)
        cap = self._s.fetch_batch_size if max_batchsize is None else max(1, min(256, int(max_batchsize)))
        active = self._s.judgehost_state_store.active_job_for_host(safe_host)
        if active is not None:
            active_job_id = int(active["job_id"])
            active_task_id = domjudge_text(active["task_id"])
            active_task_row = self._core.task_by_id(active_task_id) if active_task_id else None
            active_priority = (
                self._queue._task_priority_for_row(active_task_row)
                if active_task_row is not None
                else self._queue._task_priority_rank(
                    task_kind="",
                    verification_source=active.get("verification_source"),
                    compile_only=False,
                )
            )
            preempt_active = self._queue._queued_higher_priority_exists(active_priority) or self._s.judgehost_state_store.higher_priority_pending_job_exists(
                exclude_job_id=active_job_id,
                priority_lt=active_priority,
            )
            if preempt_active:
                now_text = now_iso()
                if self._s.judgehost_state_store.host_leased_case_count(safe_host) > 0:
                    self._queue._record_host_event_conn(hostname=safe_host, action="preempt-deferred")
                    return []
                self._s.judgehost_state_store.release_host_job_ownership(safe_host, now_text=now_text)
                self._queue._record_host_event_conn(hostname=safe_host, action="preempt")
                active = None
        if active is not None:
            active_job_id = int(active["job_id"])
            active_group_key = domjudge_text(active["group_key"])
            if active_group_key:
                active_job_id = self._domjudge_absorb_grouped_tasks(
                    job_id=active_job_id,
                    hostname=safe_host,
                    group_key=active_group_key,
                )
            leased_cases = self._domjudge_lease_cases(active_job_id, safe_host, cap)
            if leased_cases:
                return leased_cases
            self._result._domjudge_finalize_if_ready(active_job_id)
            refreshed = self._s.judgehost_state_store.active_job_for_host(safe_host)
            if refreshed is not None and int(refreshed["job_id"]) == active_job_id:
                self._queue._record_host_event_conn(hostname=safe_host, action="fetch")
                return []

        invalid_payload_drops = 0
        while True:
            leased = self._queue.fetch_work(safe_host)
            if not leased:
                shared_job = self._s.judgehost_state_store.shared_pending_job(safe_host)
                if shared_job is not None:
                    shared_job_id = int(shared_job["job_id"])
                    leased_cases = self._domjudge_lease_cases(shared_job_id, safe_host, cap)
                    if leased_cases:
                        return leased_cases
                    self._result._domjudge_finalize_if_ready(shared_job_id)
                return []
            leased_task = leased[0]
            task_id = domjudge_text(leased_task.get("task_id"))
            run_id = domjudge_text(leased_task.get("run_id"))
            try:
                leased_payload = cast(dict[str, object] | None, leased_task.get("payload")) or {}
                group_key = domjudge_text(leased_payload.get("domjudge_group_key"))
                if group_key:
                    existing_group_job = self._s.judgehost_state_store.job_for_group_key(group_key)
                    reuse_group_job = (
                        existing_group_job is not None
                        and domjudge_text(existing_group_job["lease_owner"]) in {"", safe_host}
                    )
                    if reuse_group_job:
                        active_job_id = int(existing_group_job["job_id"])
                        outcome = self._domjudge_append_task_to_job(active_job_id, leased_task)
                        if outcome == "closed":
                            active_job_id = self._domjudge_prepare_job(
                                safe_host,
                                leased_task,
                                group_key=group_key,
                            )
                    else:
                        active_job_id = self._domjudge_prepare_job(safe_host, leased_task, group_key=group_key)
                    active_job_id = self._domjudge_absorb_grouped_tasks(
                        job_id=active_job_id,
                        hostname=safe_host,
                        group_key=group_key,
                    )
                else:
                    existing_job = None
                    if task_id:
                        existing_job = self._s.judgehost_state_store.job_for_task(task_id)
                    if existing_job is None and run_id:
                        existing_job = self._s.judgehost_state_store.job_for_run(run_id)
                    if existing_job is not None:
                        active_job_id = int(existing_job["job_id"])
                        existing_task_id = domjudge_text(existing_job.get("task_id"))
                        if task_id and task_id != existing_task_id:
                            raise RuntimeError("judgehost run id already belongs to another task")
                    else:
                        active_job_id = self._domjudge_prepare_job(safe_host, leased_task)
            except Exception as exc:
                error_text = str(exc)
                if not error_text:
                    error_text = "invalid judgehost task payload"
                logger.warning("invalid judgehost task dropped task_id=%s host=%s: %s", task_id, safe_host, error_text)
                if task_id:
                    try:
                        self._queue.report_result(
                            task_id=task_id,
                            hostname=safe_host,
                            payload={
                                "run_status": "failed",
                                "error": error_text,
                                "summary": {"error": error_text},
                            },
                        )
                    except Exception as report_exc:
                        logger.warning("failed to mark invalid judgehost task as failed task_id=%s: %s", task_id, report_exc)
                invalid_payload_drops += 1
                if invalid_payload_drops >= 32:
                    return []
                continue

            self._domjudge_apply_cache_shortcuts_for_job(active_job_id, hostname=safe_host)
            leased_cases = self._domjudge_lease_cases(active_job_id, safe_host, cap)
            if leased_cases:
                return leased_cases
            self._result._domjudge_finalize_if_ready(active_job_id)
            return []
