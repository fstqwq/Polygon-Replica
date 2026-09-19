import re
from dataclasses import dataclass

from app.service.judgehost.domjudge.codec import decode_json_object, decode_text
from app.service.judgehost.domjudge.result import parse_bool, run_time_limit_sec
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


@dataclass(frozen=True, slots=True)
class CaseCachePolicy:
    """Immutable cache identity and eligibility owned by one program batch."""

    compile_config_hash: str
    run_config_hash: str
    compare_config_hash: str
    toolchain_cmd_digest: str
    time_limit_sec: float
    expected_behavior: str
    main_correct: bool
    requires_output: bool
    bypass: bool

    @classmethod
    def from_config(
        cls,
        *,
        compile_config_json: str,
        run_config_json: str,
        compare_config_json: str,
        expected_behavior: str,
        verification_source: str,
        bypass: int,
    ) -> "CaseCachePolicy":
        compile_config = decode_json_object(compile_config_json)
        run_config = decode_json_object(run_config_json)
        compare_config = decode_json_object(compare_config_json)
        digest = decode_text(raw=compile_config.get("toolchain_cmd_digest"))
        main_correct = verification_source == "main-correct"
        return cls(
            compile_config_hash=RuntimeCacheIndex.signature(compile_config),
            run_config_hash=RuntimeCacheIndex.signature(run_config),
            compare_config_hash=RuntimeCacheIndex.signature(compare_config),
            toolchain_cmd_digest=digest if re.fullmatch(r"[0-9a-f]{64}", digest) else "",
            time_limit_sec=run_time_limit_sec(run_config),
            expected_behavior=expected_behavior or "unknown",
            main_correct=main_correct,
            requires_output=main_correct or "generate-input" in verification_source,
            bypass=parse_bool(bypass, default=False),
        )
