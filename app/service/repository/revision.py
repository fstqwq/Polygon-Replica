import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Literal, TypedDict

from app.service.platform.git_process import run_git

_WORKSPACE_ORIGIN_CACHE_TTL_SEC = 10.0
_WORKSPACE_ORIGIN_CACHE: dict[str, tuple[float, Path | None]] = {}


class WorkspaceRevisionInfo(TypedDict):
    local: int | None
    upstream: int | None
    display: str
    highlight: bool
    upstream_higher: bool
    missing: bool
    ahead_count: int | None
    behind_count: int | None


@dataclass(frozen=True)
class VerificationSource:
    kind: Literal["commit", "workspace"]
    base_commit: str


_WORKSPACE_SOURCE_PREFIX = "workspace:"


def workspace_verification_source(base_commit: str | None) -> str:
    """Encode a workspace snapshot source without pretending it is a commit source."""
    safe_commit = str(base_commit or "").strip()
    return f"{_WORKSPACE_SOURCE_PREFIX}{safe_commit}" if safe_commit else "workspace"


def parse_verification_source(source_ref: str | None) -> VerificationSource:
    """Decode the persisted verification source union at the application boundary."""
    safe_source = str(source_ref or "").strip()
    if safe_source == "workspace":
        return VerificationSource("workspace", "")
    if safe_source.startswith(_WORKSPACE_SOURCE_PREFIX):
        return VerificationSource("workspace", safe_source[len(_WORKSPACE_SOURCE_PREFIX):])
    return VerificationSource("commit", safe_source)


def verification_source_display(
    workspace: Path,
    source_ref: str | None,
    revision_cache: dict[str, int | None],
) -> str:
    """Render a persisted verification source for user-facing run/export lists."""
    source = parse_verification_source(source_ref)
    if source.kind == "workspace":
        if not source.base_commit:
            return "Workspace"
        if source.base_commit not in revision_cache:
            revision_cache[source.base_commit] = git_commit_count(workspace, source.base_commit)
        revision = revision_cache[source.base_commit]
        return f"Workspace on v{revision}" if revision is not None else "Workspace on v?"
    if not source.base_commit:
        return "Workspace"
    if source.base_commit not in revision_cache:
        revision_cache[source.base_commit] = git_commit_count(workspace, source.base_commit)
    revision = revision_cache[source.base_commit]
    return f"Published v{revision}" if revision is not None else "Published v?"


def workspace_upstream_revision_display(
    workspace_revision: int | None,
    upstream_revision: int | None,
) -> str:
    workspace_text = (
        f"Workspace on v{workspace_revision}"
        if workspace_revision is not None
        else "none"
    )
    upstream_text = (
        f"v{upstream_revision}"
        if upstream_revision is not None
        else "missing"
    )
    return f"{workspace_text} / Upstream {upstream_text}"


def git_commit_count(workspace: Path, rev: str) -> int | None:
    try:
        proc = run_git(["git", "-C", str(workspace), "rev-list", "--count", str(rev)])
        if proc.returncode != 0:
            return None
        value = int(str(proc.stdout or "").strip())
        return value if value >= 0 else None
    except Exception:
        return None


def git_commit_sha(workspace: Path, rev: str) -> str | None:
    try:
        proc = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", str(rev)])
        if proc.returncode != 0:
            return None
        value = str(proc.stdout or "").strip()
        return value or None
    except Exception:
        return None


def workspace_origin_local_repo(workspace: Path) -> Path | None:
    key = str(workspace)
    cached = _WORKSPACE_ORIGIN_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and (now - float(cached[0])) <= _WORKSPACE_ORIGIN_CACHE_TTL_SEC:
        return cached[1]
    try:
        proc = run_git(["git", "-C", str(workspace), "remote", "get-url", "origin"], timeout=5)
    except Exception:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    if proc.returncode != 0:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    raw = str(proc.stdout or "").strip()
    if not raw:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    remote_path: Path | None = None
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
            return None
        decoded = unquote(parsed.path or "")
        if not decoded:
            _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
            return None
        remote_path = Path(decoded)
    elif "://" in raw:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    elif ":" in raw and (not raw.startswith("/")) and (not raw.startswith("./")) and (not raw.startswith("../")):
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    else:
        remote_path = Path(raw)
    if not remote_path.is_absolute():
        remote_path = (workspace / remote_path).resolve()
    else:
        remote_path = remote_path.resolve()
    resolved = remote_path if remote_path.exists() else None
    _WORKSPACE_ORIGIN_CACHE[key] = (now, resolved)
    return resolved


def workspace_upstream_revision_info(workspace: Path, branch: str) -> tuple[int | None, str | None]:
    upstream_ref = f"origin/{branch}"
    origin_repo = workspace_origin_local_repo(workspace)
    if origin_repo is not None:
        upstream_branch_ref = f"refs/heads/{branch}"
        version = git_commit_count(origin_repo, upstream_branch_ref)
        if version is not None:
            return (version, None)
        commit = git_commit_sha(origin_repo, upstream_branch_ref)
        if commit is not None:
            return (None, commit)
    version = git_commit_count(workspace, upstream_ref)
    if version is not None:
        return (version, None)
    return (None, git_commit_sha(workspace, upstream_ref))


def workspace_revision_info(
    workspace: Path,
    branch: str = "main",
    *,
    workspace_head: str | None = None,
    workspace_dirty: bool | None = None,
) -> WorkspaceRevisionInfo:
    safe_branch = str(branch or "main").strip() or "main"
    if any((ch.isspace() for ch in safe_branch)):
        safe_branch = "main"
    safe_head = str(workspace_head or "").strip()
    upstream_ref = f"origin/{safe_branch}"
    local_version = git_commit_count(workspace, "HEAD")
    local_commit = safe_head or git_commit_sha(workspace, "HEAD")
    upstream_version, upstream_commit = workspace_upstream_revision_info(workspace, safe_branch)
    if local_version is None and local_commit is None:
        local_version = 0
    ahead_count: int | None = None
    behind_count: int | None = None
    try:
        proc = run_git(["git", "-C", str(workspace), "rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"], timeout=30)
        if proc.returncode == 0:
            parts = str(proc.stdout or "").strip().split()
            if len(parts) >= 2:
                ahead_count = max(0, int(parts[0]))
                behind_count = max(0, int(parts[1]))
    except Exception:
        ahead_count = None
        behind_count = None
    if ahead_count is None or behind_count is None:
        if local_commit is not None and upstream_commit is not None and (local_commit == upstream_commit):
            ahead_count = 0
            behind_count = 0
        elif local_version == 0 and upstream_version == 0:
            ahead_count = 0
            behind_count = 0
    if upstream_version is None and upstream_commit is None and local_version == 0:
        upstream_version = 0
    upstream_higher = False
    if local_version is not None and upstream_version is not None:
        upstream_higher = upstream_version > local_version
    elif behind_count is not None:
        upstream_higher = behind_count > 0
    missing = local_version is None or upstream_version is None
    display = workspace_upstream_revision_display(local_version, upstream_version)
    highlight = bool(upstream_higher or missing)
    return {
        "local": local_version,
        "upstream": upstream_version,
        "display": display,
        "highlight": highlight,
        "upstream_higher": bool(upstream_higher),
        "missing": bool(missing),
        "ahead_count": ahead_count,
        "behind_count": behind_count,
    }
