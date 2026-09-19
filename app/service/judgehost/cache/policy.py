import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.service.judgehost.domjudge.codec import decode_json_object, decode_text
from app.service.judgehost.domjudge.result import parse_bool
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


def _freeze_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class CaseCachePolicy:
    """Immutable cache identity and eligibility owned by one program batch."""

    compile_config_hash: str
    run_config_hash: str
    compare_config_hash: str
    toolchain_cmd_digest: str
    run_config: Mapping[str, object]
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
            run_config=MappingProxyType({key: _freeze_value(value) for key, value in run_config.items()}),
            expected_behavior=expected_behavior or "unknown",
            main_correct=main_correct,
            requires_output=main_correct or "generate-input" in verification_source,
            bypass=parse_bool(bypass, default=False),
        )
