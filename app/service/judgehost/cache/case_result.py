from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.service.execution.codec import execution_result_from_json
from app.service.judgehost.batch.model import CaseResult
from app.service.judgehost.domjudge.cache import case_cache_ref
from app.service.judgehost.domjudge.codec import decode_text
from app.service.judgehost.domjudge.result import (
    parse_bool,
    parse_nonnegative_float,
    rewrite_untrusted_runresult,
    verdict_from_runresult,
)
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.platform.runtime_cache_index import (
    RuntimeCacheConflictError,
    RuntimeCacheIndex,
)


@dataclass(frozen=True, slots=True)
class CaseResultStoreOutcome:
    """Result of publishing an optional, first-writer-wins case cache entry."""

    status: Literal["stored", "unchanged", "conflict"]
    files: dict[str, PayloadFile]


@dataclass(frozen=True, slots=True)
class CaseCacheLookup:
    source_hash: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    compile_config_hash: str
    run_config_hash: str
    compare_config_hash: str
    toolchain_cmd_digest: str
    testcase_hash: str
    run_config: dict[str, object]
    expected_behavior: str
    main_correct: bool
    requires_output: bool
    bypass: bool


class CaseResultCache:
    """Own result-cache identity, validation, lookup, and storage."""

    def __init__(self, index: RuntimeCacheIndex, blobs: RuntimeBlobStore) -> None:
        self._index = index
        self._blobs = blobs

    @staticmethod
    def identity(lookup: CaseCacheLookup) -> tuple[str, str]:
        return case_cache_ref(
            source_hash=lookup.source_hash,
            compile_hash=lookup.compile_hash,
            run_hash=lookup.run_hash,
            compare_hash=lookup.compare_hash,
            compile_config_hash=lookup.compile_config_hash,
            run_config_hash=lookup.run_config_hash,
            compare_config_hash=lookup.compare_config_hash,
            toolchain_cmd_digest=lookup.toolchain_cmd_digest,
            testcase_hash=lookup.testcase_hash,
        )

    def delete(self, key_hash: str, signature: str) -> None:
        self._index.delete(
            namespace=RuntimeCacheIndex.RESULT,
            key_hash=key_hash,
            signature=signature,
        )

    def lookup(self, lookup: CaseCacheLookup) -> CaseResult | None:
        key_hash, signature = self.identity(lookup)
        if lookup.bypass:
            self.delete(key_hash, signature)
            return None
        entry = self._index.get(
            namespace=RuntimeCacheIndex.RESULT,
            key_hash=key_hash,
            signature=signature,
        )
        if entry is None:
            return None
        value = dict(entry.value)
        if not parse_bool(value.get("shortcut_eligible"), default=True):
            return None
        runresult = rewrite_untrusted_runresult(
            decode_text(raw=value.get("runresult")),
            cpu_sec=parse_nonnegative_float(
                value.get("cpu_sec"),
                parse_nonnegative_float(value.get("runtime_sec"), 0.0),
            ),
            run_cfg_obj=lookup.run_config,
        )
        verdict = verdict_from_runresult(runresult)
        if verdict == "FL":
            self.delete(key_hash, signature)
            return None
        if (
            lookup.main_correct or lookup.expected_behavior in {"accepted", "compile"}
        ) and verdict != "OK":
            if lookup.expected_behavior == "compile":
                self.delete(key_hash, signature)
            return None
        result_json = decode_text(raw=value.get("result_json"))
        if not result_json:
            self.delete(key_hash, signature)
            return None
        result = execution_result_from_json(result_json)
        if any(self._blobs.descriptor(token) is None for token in result.artifact_refs()):
            self.delete(key_hash, signature)
            return None
        if verdict == "OK" and lookup.expected_behavior != "compile" and not result.output_run_ref:
            self.delete(key_hash, signature)
            return None
        if lookup.requires_output and not result.output_run_ref:
            self.delete(key_hash, signature)
            return None
        return result

    def store(
        self,
        *,
        key_hash: str,
        signature: str,
        tags: dict[str, object],
        runresult: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        score_text: str,
        result_json: str,
        files: Mapping[str, bytes | PayloadFile],
        shortcut_eligible: bool,
    ) -> dict[str, PayloadFile]:
        entry = self._index.put(
            namespace=RuntimeCacheIndex.RESULT,
            key_hash=key_hash,
            signature=signature,
            value={
                "runresult": decode_text(lower=True, raw=runresult),
                "runtime_sec": max(0.0, runtime_sec),
                "cpu_sec": max(0.0, cpu_sec),
                "wall_sec": max(0.0, wall_sec),
                "memory_kb": max(0, memory_kb),
                "score_text": decode_text(raw=score_text),
                "result_json": result_json,
                "shortcut_eligible": shortcut_eligible,
            },
            files=files,
            tags=tags,
        )
        return dict(entry.files)

    def try_store(
        self,
        *,
        key_hash: str,
        signature: str,
        tags: dict[str, object],
        runresult: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        score_text: str,
        result_json: str,
        files: Mapping[str, bytes | PayloadFile],
        shortcut_eligible: bool,
    ) -> CaseResultStoreOutcome:
        """Publish a result cache entry without replacing the first writer.

        Result-cache payloads contain volatile execution measurements, so an
        identity collision is an expected race between equivalent executions.
        The strict ``RuntimeCacheIndex`` invariant remains intact; this method
        simply makes the result-cache publication boundary optional to callers.
        """
        existing = self._index.get(
            namespace=RuntimeCacheIndex.RESULT,
            key_hash=key_hash,
            signature=signature,
        )
        try:
            stored = self.store(
                key_hash=key_hash,
                signature=signature,
                tags=tags,
                runresult=runresult,
                runtime_sec=runtime_sec,
                cpu_sec=cpu_sec,
                wall_sec=wall_sec,
                memory_kb=memory_kb,
                score_text=score_text,
                result_json=result_json,
                files=files,
                shortcut_eligible=shortcut_eligible,
            )
        except RuntimeCacheConflictError:
            return CaseResultStoreOutcome("conflict", {})
        return CaseResultStoreOutcome(
            "unchanged" if existing is not None else "stored",
            stored,
        )
