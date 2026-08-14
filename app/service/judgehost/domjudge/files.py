"""Serve immutable DOMjudge source, testcase, and executable payloads."""

import logging
from dataclasses import dataclass

from app.db import now_iso
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.cache.executable import ExecutableCache
from app.service.judgehost.domjudge.codec import decode_contest_id, decode_text
from app.service.judgehost.domjudge.file_stream import DomjudgeDownloadFile
from app.service.judgehost.domjudge.identity import (
    parse_script_id,
    script_hash_field,
    script_id,
)
from app.service.judgehost.validation import normalize_judgehost_hostname
from app.service.platform.runtime_blob_store import RuntimeBlobStore

logger = logging.getLogger(__name__)
diagnostic_logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True, slots=True)
class ExecutableFileOutcome:
    files: tuple[DomjudgeDownloadFile, ...]
    terminal_batch_ids: tuple[int, ...]
    error: str


class DomjudgeFileService:
    """Resolve protocol file identities without owning callback lifecycle."""

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        runtime_blob_store: RuntimeBlobStore,
        executable_cache: ExecutableCache,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._runtime_blob_store = runtime_blob_store
        self._executable_cache = executable_cache

    def source_files(
        self,
        submit_id: str,
        contest_id: str | None = None,
    ) -> list[DomjudgeDownloadFile]:
        safe_submit = decode_text(raw=submit_id)
        if not safe_submit:
            raise RuntimeError("source files not found")
        safe_contest = None if contest_id is None else decode_contest_id(contest_id)
        submission = self._batch_runtime.source_submission(
            safe_submit,
            contest_id=safe_contest,
        )
        if submission is None:
            raise RuntimeError("source files not found")
        return [
            DomjudgeDownloadFile(filename, payload)
            for filename, payload in (
                (submission.source_name, submission.source_file),
                *submission.extra_source_items,
            )
        ]

    def testcase_files(self, testcase_id: int) -> list[DomjudgeDownloadFile]:
        token = int(testcase_id)
        row, resolution_source = self._batch_runtime.testcase_refs(token)
        if row is None:
            diagnostic_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s resolved=missing",
                token,
            )
            raise RuntimeError("testcase files not found")
        input_ref = decode_text(raw=row["input_ref"])
        answer_ref = decode_text(raw=row["answer_ref"])
        input_file = self._runtime_blob_store.descriptor(input_ref)
        answer_file = self._runtime_blob_store.descriptor(answer_ref)
        if input_file is None or answer_file is None:
            diagnostic_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s resolved=%s "
                "exists=%s input=%s answer=%s",
                token,
                resolution_source,
                False,
                input_ref,
                answer_ref,
            )
            raise RuntimeError("testcase files not found")
        logger.debug(
            "judgehost.get_testcase_files testcase_id=%s resolved=%s "
            "exists=%s input=%s answer=%s",
            token,
            resolution_source,
            True,
            input_ref,
            answer_ref,
        )
        return [
            DomjudgeDownloadFile("input", input_file),
            DomjudgeDownloadFile("output", answer_file),
        ]

    def _executable_rows(
        self,
        *,
        kind: str,
        executable_hash: str,
    ) -> list[DomjudgeDownloadFile]:
        cached_rows = self._executable_cache.read(
            kind=kind,
            executable_hash=executable_hash,
        )
        if not cached_rows:
            raise RuntimeError("script files not found")
        return [
            DomjudgeDownloadFile(
                row.filename,
                row.payload,
                row.is_executable,
            )
            for row in cached_rows
        ]

    def _active_batch_script_hash(
        self,
        *,
        hostname: str,
        kind: str,
        requested_id: int,
    ) -> tuple[int, str] | None:
        safe_host = normalize_judgehost_hostname(hostname)
        leased_match = self._batch_runtime.leased_script_hash_for_host(
            safe_host,
            kind=kind,
            script_id=requested_id,
        )
        if leased_match is not None:
            return leased_match
        matching: dict[int, str] = {}
        for batch_row in self._batch_runtime.host_context_batches(safe_host):
            if kind == "compile":
                script_hash = batch_row["compile_hash"]
            elif kind == "run":
                script_hash = batch_row["run_hash"]
            elif kind == "compare":
                script_hash = batch_row["compare_hash"]
            else:
                raise RuntimeError("invalid script kind")
            if script_hash and script_id(script_hash) == requested_id:
                matching[int(batch_row["batch_id"])] = script_hash
        matching_hashes = set(matching.values())
        if len(matching_hashes) != 1:
            return None
        script_hash = next(iter(matching_hashes))
        batch_id = next(
            batch_id for batch_id, value in matching.items() if value == script_hash
        )
        return (batch_id, script_hash)

    def _shared_script_hash(self, *, kind: str, requested_id: int) -> str:
        matching_hashes = self._batch_runtime.active_script_hashes(kind, requested_id)
        if not matching_hashes:
            raise RuntimeError("script files not found")
        if len(matching_hashes) > 1:
            raise RuntimeError("ambiguous script id")
        return next(iter(matching_hashes))

    def _fail_batch_executable_lookup(self, *, batch_id: int, error_text: str) -> None:
        safe_error = decode_text(raw=error_text) or "judgehost executable cache missing"
        now_text = now_iso()
        self._batch_runtime.append_debug_text(
            case_id=None,
            batch_id=int(batch_id),
            debug_text=safe_error,
            now_text=now_text,
        )
        self._batch_runtime.record_batch_failure(
            int(batch_id),
            runresult="internal-error",
            error_text=safe_error,
            updated_at=now_text,
        )

    def executable_files(
        self,
        kind: str,
        script_identity: object,
        *,
        hostname: str = "",
    ) -> ExecutableFileOutcome:
        requested_id = parse_script_id(script_identity)
        token = decode_text(lower=True, raw=kind)
        script_hash_field(token)
        active_match = (
            None
            if not hostname
            else self._active_batch_script_hash(
                hostname=hostname,
                kind=token,
                requested_id=requested_id,
            )
        )
        if active_match is not None:
            batch_id, executable_hash = active_match
            try:
                return ExecutableFileOutcome(
                    files=tuple(
                        self._executable_rows(
                            kind=token,
                            executable_hash=executable_hash,
                        )
                    ),
                    terminal_batch_ids=(),
                    error="",
                )
            except RuntimeError as exc:
                error_text = (
                    "judgehost executable cache missing: "
                    f"{token}/{requested_id}"
                )
                self._fail_batch_executable_lookup(
                    batch_id=batch_id,
                    error_text=error_text,
                )
                return ExecutableFileOutcome(
                    files=(),
                    terminal_batch_ids=(batch_id,),
                    error=f"{error_text}: {exc}",
                )
        return ExecutableFileOutcome(
            files=tuple(
                self._executable_rows(
                    kind=token,
                    executable_hash=self._shared_script_hash(
                        kind=token,
                        requested_id=requested_id,
                    ),
                )
            ),
            terminal_batch_ids=(),
            error="",
        )
