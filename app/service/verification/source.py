from pathlib import Path

from app.service.problem.build_config import BuildConfig
from app.service.problem.source_file import resolve_source

def select_source(
    snapshot: Path,
    build_cfg: BuildConfig,
    config_key: str,
    folder: str,
    *,
    cpp_extensions: tuple[str, ...],
    snapshot_resolved: Path | None = None,
) -> Path | None:
    configured = build_cfg.get(config_key)
    if configured is None:
        return None
    source = resolve_source(snapshot, str(configured))
    relative = source.relative_to(snapshot_resolved or snapshot.resolve())
    if not relative.parts or relative.parts[0] != folder:
        raise RuntimeError(f"configured source must be below {folder}/")
    if source.suffix.lower() not in cpp_extensions:
        allowed = "/".join(cpp_extensions)
        raise RuntimeError(f"configured source must use one of: {allowed}")
    return source
