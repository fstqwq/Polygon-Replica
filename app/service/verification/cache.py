from __future__ import annotations

from pathlib import Path

from app.service.platform.fs.layout import FsManager
from app.service.platform.hashing import canonical_json, sha256_hex_text


def canonical_digest(payload: object) -> str:
    text = canonical_json(payload, ensure_ascii=False)
    return sha256_hex_text(text)


def verification_cache_key_hash(key_obj: dict[str, object]) -> str:
    text = canonical_json(key_obj, ensure_ascii=False)
    return sha256_hex_text(text)


def artifact_ref_from_cache_key_hash(fs_manager: FsManager, *, schema: str, cache_key_hash: str) -> str:
    digest = str(cache_key_hash or "").strip().lower()
    if not digest:
        digest = sha256_hex_text(f"{schema}:empty")
    return fs_manager.compute_artifact_ref(
        {
            "schema": str(schema or "v3"),
            "cache_key_hash": digest,
        }
    )


def artifact_root_from_ref(fs_manager: FsManager, *, artifact_ref: str) -> Path:
    return fs_manager.artifact_paths(str(artifact_ref or "").strip().lower()).root.resolve()


def ensure_artifact_paths(fs_manager: FsManager, *, artifact_ref: str):
    return fs_manager.ensure_artifact_layout(str(artifact_ref or "").strip().lower())


def verification_cache_key(
    *,
    schema: str,
    problem_id: int,
    workspace_id: int,
    source_commit: str,
    source_ref: str,
    generation_params_digest: str,
    toolchain_cmd_digest: str,
    sample_only: bool = False,
) -> dict[str, object]:
    return {
        "problem_id": int(problem_id),
        "workspace_id": int(workspace_id),
        "source_commit": str(source_commit or "").strip(),
        "source_ref": str(source_ref or "").strip(),
        "generation_params_digest": str(generation_params_digest or "").strip().lower(),
        "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
        "sample_only": bool(sample_only),
        "schema": str(schema or "v3"),
    }


