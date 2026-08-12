import app.main_constant as _K

import json
import os
import re
import time
import uuid
from pathlib import Path

from app.db import now_iso
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_operation import normalize_contest_slug_required

"""
Boundary:
- Owns contest import draft/slug helpers used by root auth routes.
- Does not own route handlers, request parsing, or template rendering.

Invariants:
- Keep contest/problem slug normalization and collision behavior unchanged.
- Keep draft file layout/TTL/path-safety checks unchanged.
"""

_CONTEST_IMPORT_SUFFIX_RE = re.compile(r"-\d+$")
_CONTEST_IMPORT_DRAFT_ID_RE = re.compile(r"^[a-f0-9]{24}$")
_CONTEST_IMPORT_DRAFT_TTL_SEC = 6 * 60 * 60
_PROBLEM_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _slugify_contest_id(raw: str) -> str:
    token = raw.strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > 64:
        token = token[:64].rstrip("-")
    return token


def _import_contest_slug_base_from_package_name(package_name: str) -> str:
    raw_stem = Path(package_name or "imported-contest.zip").stem.strip()
    normalized_stem = _CONTEST_IMPORT_SUFFIX_RE.sub("", raw_stem).strip()
    if not normalized_stem:
        normalized_stem = raw_stem
    slug = _slugify_contest_id(normalized_stem)
    base = slug or "imported-contest"
    if not _K.CONTEST_IDENT_RE.fullmatch(base):
        return "imported-contest"
    return base


def _next_available_contest_slug(base: str) -> str:
    token = base.strip() or "imported-contest"
    candidate = token
    idx = 2
    while runtime().contest_service.contest_slug_exists(candidate):
        suffix = f"-{idx}"
        prefix_len = max(1, 64 - len(suffix))
        prefix = token[:prefix_len].rstrip("-") or "c"
        candidate = f"{prefix}{suffix}"
        idx += 1
    return candidate


def _resolve_import_contest_slug(requested_slug: str, package_name: str) -> str:
    requested = requested_slug.strip()
    if requested:
        slug = normalize_contest_slug_required(requested)
        if runtime().contest_service.contest_slug_exists(slug):
            suggestion = _next_available_contest_slug(slug)
            raise ValueError(f"contest slug already exists: {slug} (try: {suggestion})")
        return slug
    base = _import_contest_slug_base_from_package_name(package_name)
    return _next_available_contest_slug(base)


def _contest_idx_label(seq: int) -> str:
    value = max(1, int(seq))
    chars: list[str] = []
    while value > 0:
        value -= 1
        chars.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(chars))


def _normalize_import_contest_idx(raw: object, seq: int, used: set[str]) -> str:
    token = str(raw or "").strip().upper()
    if token and len(token) <= 16 and _K.CONTEST_IDENT_RE.fullmatch(token) and token not in used:
        used.add(token)
        return token
    candidate_seq = max(1, int(seq))
    while True:
        candidate = _contest_idx_label(candidate_seq)
        if candidate not in used:
            used.add(candidate)
            return candidate
        candidate_seq += 1


def _contest_import_draft_root() -> Path:
    root = runtime().storage_layout.contest_import_draft_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_contest_import_draft_id(raw: str) -> str:
    token = raw.strip().lower()
    if not _CONTEST_IMPORT_DRAFT_ID_RE.fullmatch(token):
        raise ValueError("invalid contest import draft id")
    return token


def _contest_import_draft_paths(draft_id: str) -> tuple[Path, Path]:
    safe_id = _safe_contest_import_draft_id(draft_id)
    root = _contest_import_draft_root()
    meta_path = (root / f"{safe_id}.json").resolve()
    payload_path = (root / f"{safe_id}.zip").resolve()
    if root not in meta_path.parents or root not in payload_path.parents:
        raise ValueError("invalid contest import draft path")
    return meta_path, payload_path


def _cleanup_stale_contest_import_drafts() -> None:
    root = _contest_import_draft_root()
    deadline = time.time() - float(_CONTEST_IMPORT_DRAFT_TTL_SEC)
    try:
        for meta in root.glob("*.json"):
            if meta.is_symlink() or (not meta.is_file()):
                continue
            stem = meta.stem.strip().lower()
            if not _CONTEST_IMPORT_DRAFT_ID_RE.fullmatch(stem):
                continue
            try:
                st = meta.stat()
            except OSError:
                continue
            if float(st.st_mtime) >= deadline:
                continue
            _, payload = _contest_import_draft_paths(stem)
            meta.unlink(missing_ok=True)
            payload.unlink(missing_ok=True)
    except OSError:
        return

def _problem_slug_segment_max_len(owner: str) -> int:
    safe_owner = owner.strip().lower()
    if not _K.USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(_K.USERNAME_RULE_MESSAGE)
    return max(1, int(_K.PROBLEM_ID_MAX_LEN) - len(safe_owner) - 1)


def _slugify_problem_id(raw: str, *, max_len: int) -> str:
    token = raw.strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > max_len:
        token = token[:max_len].rstrip("-")
    return token


def _normalize_problem_slug_segment_required(owner: str, raw: str) -> str:
    token = _slugify_problem_id(raw, max_len=_problem_slug_segment_max_len(owner))
    if not token or (not _PROBLEM_SEGMENT_RE.fullmatch(token)):
        raise ValueError(_K.PROBLEM_ID_RULE_MESSAGE)
    return token


def _problem_full_slug(owner: str, slug_segment: str) -> str:
    safe_owner = owner.strip().lower()
    if not _K.USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(_K.USERNAME_RULE_MESSAGE)
    safe_segment = _normalize_problem_slug_segment_required(safe_owner, slug_segment)
    full_slug = f"{safe_owner}/{safe_segment}"
    if len(full_slug) > _K.PROBLEM_ID_MAX_LEN:
        raise ValueError(_K.PROBLEM_ID_RULE_MESSAGE)
    return full_slug


def _next_available_problem_slug(owner: str, base: str, reserved: set[str] | None = None) -> str:
    token = base.strip()
    if not token:
        token = "imported-problem"
    token = _normalize_problem_slug_segment_required(owner, token)
    max_len = _problem_slug_segment_max_len(owner)
    seen = set(reserved or set())
    candidate = token
    idx = 2
    while (candidate in seen) or (
        runtime().workspace_service.known_problem_id(_problem_full_slug(owner, candidate)) is not None
    ):
        suffix = f"-{idx}"
        prefix_len = max(1, max_len - len(suffix))
        prefix = token[:prefix_len].rstrip("-") or "p"
        candidate = f"{prefix}{suffix}"
        idx += 1
    return candidate


def _build_contest_import_problem_draft_rows(owner: str, parsed_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    reserved: set[str] = set()
    for seq, raw in enumerate(parsed_rows, start=1):
        row = dict(raw) if isinstance(raw, dict) else {}
        source_slug_obj = row.get("source_slug")
        source_slug = _slugify_problem_id(
            str(source_slug_obj) if source_slug_obj is not None else "",
            max_len=_problem_slug_segment_max_len(owner),
        )
        if not source_slug:
            source_slug = f"problem-{seq}"
        package_name_obj = row.get("package_name")
        package_name = str(package_name_obj).strip() if package_name_obj is not None else ""
        if not package_name:
            package_name = f"{source_slug}.zip"
        index_obj = row.get("index")
        index = str(index_obj).strip().upper() if index_obj is not None else ""
        if not index:
            index = _contest_idx_label(seq)
        suggested = _next_available_problem_slug(owner, source_slug, reserved=reserved)
        reserved.add(suggested)
        rows.append(
            {
                "seq": seq,
                "index": index,
                "source_slug": source_slug,
                "package_name": package_name,
                "suggested_slug": suggested,
            }
        )
    return rows


def _create_contest_import_draft(
    *,
    actor_user_id: int,
    actor_username: str,
    package_name: str,
    package_path: Path,
    contest_slug_input: str,
    contest_title_input: str,
    parsed_title: str,
    problem_rows: list[dict[str, object]],
) -> str:
    _cleanup_stale_contest_import_drafts()
    draft_id = uuid.uuid4().hex[:24]
    meta_path, payload_path = _contest_import_draft_paths(draft_id)
    source = Path(package_path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("contest package upload is unavailable")
    try:
        os.replace(source, payload_path)
        payload_stat = payload_path.stat()
        meta = {
            "draft_id": draft_id,
            "actor_user_id": int(actor_user_id),
            "actor_username": actor_username.strip(),
            "package_name": package_name.strip(),
            "package_size": int(payload_stat.st_size),
            "contest_slug_input": contest_slug_input.strip(),
            "contest_title_input": contest_title_input.strip(),
            "parsed_title": parsed_title.strip(),
            "problem_rows": [dict(row) for row in problem_rows],
            "created_at": now_iso(),
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        meta_path.unlink(missing_ok=True)
        payload_path.unlink(missing_ok=True)
        raise
    return draft_id


def _load_contest_import_draft(actor_user_id: int, actor_username: str, draft_id: str) -> tuple[dict[str, object], Path]:
    meta_path, payload_path = _contest_import_draft_paths(draft_id)
    if not meta_path.exists() or not meta_path.is_file() or meta_path.is_symlink():
        raise ValueError("contest import draft not found")
    if not payload_path.exists() or not payload_path.is_file() or payload_path.is_symlink():
        raise ValueError("contest import payload not found")
    try:
        meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid contest import draft metadata: {exc}") from exc
    if not isinstance(meta_raw, dict):
        raise ValueError("invalid contest import draft metadata")
    owner_id_obj = meta_raw.get("actor_user_id")
    owner_id = int(owner_id_obj) if owner_id_obj is not None else 0
    owner_name = meta_raw.get("actor_username")
    if not isinstance(owner_name, str):
        owner_name = ""
    else:
        owner_name = owner_name.strip()
    if owner_id != int(actor_user_id) or owner_name != actor_username.strip():
        raise ValueError("contest import draft owner mismatch")
    return dict(meta_raw), payload_path


def _delete_contest_import_draft(draft_id: str) -> None:
    try:
        meta_path, payload_path = _contest_import_draft_paths(draft_id)
    except ValueError:
        return
    meta_path.unlink(missing_ok=True)
    payload_path.unlink(missing_ok=True)


def _rollback_imported_problem(problem_slug: str) -> None:
    safe_slug = str(problem_slug or "").strip()
    if not safe_slug:
        return
    try:
        runtime().workspace_service.delete_problem(safe_slug)
        return
    except Exception:
        pass

    runtime().workspace_service.delete_problem(safe_slug)


def _rollback_imported_contest(contest_slug: str, imported_problem_slugs: list[str]) -> None:
    safe_slug = str(contest_slug or "").strip()
    for problem_slug in reversed(imported_problem_slugs):
        _rollback_imported_problem(problem_slug)
    if safe_slug:
        runtime().contest_service.delete_contest(safe_slug)


def _build_problem_slug_review_rows(
    owner: str,
    draft_rows: list[dict[str, object]],
    requested_overrides: dict[int, str],
) -> tuple[list[dict[str, object]], bool]:
    rows: list[dict[str, object]] = []
    requested_tokens: list[str] = []
    for row in draft_rows:
        seq_obj = row.get("seq")
        seq = int(seq_obj) if seq_obj is not None else 0
        fallback_obj = row.get("suggested_slug")
        fallback = str(fallback_obj).strip() if fallback_obj is not None else ""
        requested_raw = requested_overrides.get(seq, fallback)
        requested = _slugify_problem_id(
            str(requested_raw).strip().lower(),
            max_len=_problem_slug_segment_max_len(owner),
        )
        requested_tokens.append(requested)
    duplicate_counts: dict[str, int] = {}
    for token in requested_tokens:
        if not token:
            continue
        duplicate_counts[token] = int(duplicate_counts.get(token, 0)) + 1
    has_error = False
    for idx, row in enumerate(draft_rows):
        seq_obj = row.get("seq")
        seq = int(seq_obj) if seq_obj is not None else 0
        requested = requested_tokens[idx]
        valid = bool(requested and _PROBLEM_SEGMENT_RE.fullmatch(requested))
        full_requested = _problem_full_slug(owner, requested) if valid else ""
        exists = bool(full_requested and (runtime().workspace_service.known_problem_id(full_requested) is not None))
        duplicate = bool(requested and int(duplicate_counts.get(requested, 0)) > 1)
        message = ""
        if not requested:
            message = "slug is required"
        elif not valid:
            message = _K.PROBLEM_ID_RULE_MESSAGE
        elif duplicate:
            message = "slug duplicated in this import"
        elif exists:
            message = f"problem already exists: {full_requested}"
        ok = bool(requested) and valid and (not duplicate) and (not exists)
        if not ok:
            has_error = True
        suggested = ""
        if not ok:
            base = requested if valid else _slugify_problem_id(
                requested,
                max_len=_problem_slug_segment_max_len(owner),
            )
            if not base:
                source_slug_obj = row.get("source_slug")
                base = str(source_slug_obj).strip() if source_slug_obj is not None else ""
            suggested = _next_available_problem_slug(owner, base)
        rows.append(
            {
                "seq": seq,
                "index": str(row.get("index")).strip().upper() if row.get("index") is not None else "",
                "source_slug": str(row.get("source_slug")).strip() if row.get("source_slug") is not None else "",
                "package_name": str(row.get("package_name")).strip() if row.get("package_name") is not None else "",
                "slug_input": requested,
                "slug_full": full_requested,
                "valid": valid,
                "exists": exists,
                "duplicate": duplicate,
                "ok": ok,
                "message": message,
                "suggested": suggested,
            }
        )
    return rows, has_error


def _contest_slug_review_state(raw_slug: str, package_name: str) -> dict[str, object]:
    requested = raw_slug.strip()
    if not requested:
        base = _import_contest_slug_base_from_package_name(package_name)
        suggested = _next_available_contest_slug(base)
        return {
            "requested": "",
            "valid": True,
            "exists": False,
            "suggested": suggested,
            "message": "",
        }
    try:
        normalized = normalize_contest_slug_required(requested)
    except ValueError as exc:
        return {
            "requested": requested,
            "valid": False,
            "exists": False,
            "suggested": _next_available_contest_slug(_import_contest_slug_base_from_package_name(package_name)),
            "message": str(exc),
        }
    exists = runtime().contest_service.contest_slug_exists(normalized)
    suggested = _next_available_contest_slug(normalized) if exists else normalized
    return {
        "requested": normalized,
        "valid": True,
        "exists": bool(exists),
        "suggested": suggested,
        "message": f"contest slug already exists: {normalized}" if exists else "",
    }
