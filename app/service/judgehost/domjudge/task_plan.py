import json

from app.service.judgehost.domjudge.codec import decode_text
from app.service.judgehost.domjudge.result import parse_bool
from app.service.platform.hashing import sha256_hex_text

TASK_KIND_COMPILE_ONLY = "compile-only"
TASK_KIND_GENERATE_INPUT = "generate-input"
TASK_KIND_MAIN_CORRECT = "main-correct"
TASK_KIND_SOLUTION_RUN = "solution-run"
_TASK_KINDS = frozenset(
    {
        TASK_KIND_COMPILE_ONLY,
        TASK_KIND_GENERATE_INPUT,
        TASK_KIND_MAIN_CORRECT,
        TASK_KIND_SOLUTION_RUN,
    }
)


def force_cpp_define(source_bytes: bytes) -> bytes:
    if not source_bytes:
        return b""
    if b"#define DOMJUDGE" in source_bytes or b"# define DOMJUDGE" in source_bytes:
        return source_bytes
    return b"#ifndef DOMJUDGE\n#define DOMJUDGE 1\n#endif\n" + source_bytes


def task_kind(
    payload: dict[str, object] | None = None,
    *,
    verification_source: str | None = None,
    compile_only: object | None = None,
) -> str:
    payload_obj = {} if payload is None else payload
    explicit = decode_text(lower=True, raw=payload_obj.get("task_kind"))
    if explicit == "generate":
        explicit = TASK_KIND_GENERATE_INPUT
    if explicit in _TASK_KINDS:
        return explicit
    source = decode_text(
        lower=True,
        raw=(
            verification_source
            if verification_source is not None
            else payload_obj.get("verification_source")
        ),
    )
    compile_only_flag = parse_bool(
        compile_only if compile_only is not None else payload_obj.get("compile_only"),
        default=False,
    )
    if compile_only_flag:
        return TASK_KIND_COMPILE_ONLY
    if source == TASK_KIND_GENERATE_INPUT or source.endswith(
        f".{TASK_KIND_GENERATE_INPUT}"
    ):
        return TASK_KIND_GENERATE_INPUT
    if source == TASK_KIND_MAIN_CORRECT or source.endswith(
        f".{TASK_KIND_MAIN_CORRECT}"
    ):
        return TASK_KIND_MAIN_CORRECT
    return TASK_KIND_SOLUTION_RUN


def execution_modes(
    payload: dict[str, object] | None = None,
    *,
    verification_source: str | None = None,
    compile_only: object | None = None,
) -> tuple[bool, bool, bool]:
    kind = task_kind(
        payload,
        verification_source=verification_source,
        compile_only=compile_only,
    )
    return (
        kind == TASK_KIND_COMPILE_ONLY,
        kind == TASK_KIND_GENERATE_INPUT,
        kind == TASK_KIND_MAIN_CORRECT,
    )


def execution_mode(payload: dict[str, object]) -> str:
    kind = task_kind(payload)
    if kind == TASK_KIND_COMPILE_ONLY:
        return "pass-fail"
    verification_payload = payload.get("verification_payload")
    if not isinstance(verification_payload, dict):
        raise RuntimeError("verification payload is required for execution mode")
    problem_mode = verification_payload.get("problem_mode")
    if not isinstance(problem_mode, str) or problem_mode not in {
        "pass-fail",
        "interactive",
    }:
        raise RuntimeError(
            "verification payload problem_mode must be 'pass-fail' or 'interactive'"
        )
    if kind == TASK_KIND_GENERATE_INPUT:
        return "pass-fail"
    return problem_mode


def execution_signature(payload: dict[str, object]) -> str:
    precomputed = payload.get("precomputed")
    if not isinstance(precomputed, dict):
        return ""
    hashes = {
        name: decode_text(lower=True, raw=precomputed.get(name))
        for name in ("compile_hash", "run_hash", "compare_hash", "source_hash")
    }
    if any(not value for value in hashes.values()):
        return ""
    configs: dict[str, object] = {}
    config_hashes: dict[str, str] = {}
    for name in ("compile_config", "run_config", "compare_config"):
        value = precomputed.get(name)
        configs[name] = {} if value is None else value
        config_hashes[f"{name}_hash"] = sha256_hex_text(
            json.dumps(
                configs[name],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    compile_config = configs["compile_config"]
    toolchain_digest = (
        decode_text(lower=True, raw=compile_config.get("toolchain_cmd_digest"))
        if isinstance(compile_config, dict)
        else ""
    )
    signature_payload = {
        "task_kind": task_kind(payload),
        "verification_source": decode_text(
            lower=True, raw=payload.get("verification_source")
        ),
        "expected_behavior": decode_text(
            lower=True, raw=payload.get("expected_behavior")
        ),
        "bypass_case_result_cache": parse_bool(
            payload.get("bypass_case_result_cache"), default=False
        ),
        **hashes,
        **config_hashes,
        "toolchain_cmd_digest": toolchain_digest,
    }
    return sha256_hex_text(
        json.dumps(
            signature_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
