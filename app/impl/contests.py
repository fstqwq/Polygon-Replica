from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.db import now_iso
from app.impl.auth import _redirect_response, _template_response
from app.impl.config import config
from app.impl.workspace import (
    _audit,
    _coerce_int,
    _form_text,
    _global_user_ctx,
    _latest_workspace_committed_build,
    _normalize_contest_role,
    _normalize_contest_slug_required,
    _normalize_contest_title_required,
    _normalize_problem_mode,
    _normalize_problem_name_required,
    _read_problem_config,
    _workspace_access_context,
    _workspace_revision_info,
)
from app.services.hashing import sha256_file
from app.services.util import is_canonical_artifact_id, run_cmd

_C = config.constants

_CONTEST_PROPERTY_SOURCE_MODE = "source_mode"
_CONTEST_PROPERTY_LOCATION = "location"
_CONTEST_PROPERTY_DATE = "date"
_CONTEST_SOURCE_MODE_VALUES = {"latest_committed", "built_packages"}
_CONTEST_JOB_TYPE_PREVIEW = "preview"
_CONTEST_JOB_TYPE_PACKAGE = "package"
_CONTEST_ARTIFACTS_BUCKET = "__contests__"


def _contest_idx_label(seq: int) -> str:
    value = max(1, int(seq))
    chars: list[str] = []
    while value > 0:
        value -= 1
        chars.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(chars))


def _normalize_contest_problem_idx_required(raw: object) -> str:
    token = str(raw or "").strip().upper()
    if not token:
        raise ValueError("problem index is required")
    if len(token) > 16:
        raise ValueError("problem index is too long")
    if not _C.CONTEST_IDENT_RE.fullmatch(token):
        raise ValueError("invalid problem index")
    return token


def _normalize_contest_member_role_required(raw: object) -> str:
    role = str(raw or "").strip().lower()
    if role not in {"owner", "write", "read"}:
        raise ValueError("invalid role")
    return role


def _dedupe_preserve(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _contest_access_context(contest_id: int, user_id: int) -> dict:
    row = config.db.fetch_one(
        "SELECT role FROM contest_members WHERE contest_id=? AND user_id=?",
        [int(contest_id), int(user_id)],
    )
    if row is None:
        return {
            "role": "none",
            "can_read": False,
            "can_write": False,
            "can_manage": False,
            "read_block_reason": "you do not have access to this contest",
            "write_block_reason": "write access required",
            "manage_block_reason": "owner access required",
        }
    role = _normalize_contest_role(row["role"])
    can_write = role in {"owner", "write"}
    return {
        "role": role,
        "can_read": True,
        "can_write": can_write,
        "can_manage": role == "owner",
        "read_block_reason": "",
        "write_block_reason": "" if can_write else "read-only access",
        "manage_block_reason": "" if role == "owner" else "owner access required",
    }


def _contest_owner_count(contest_id: int) -> int:
    row = config.db.fetch_one(
        "SELECT COUNT(*) AS c FROM contest_members WHERE contest_id=? AND role='owner'",
        [int(contest_id)],
    )
    if row is None:
        return 0
    try:
        return max(0, int(row["c"] or 0))
    except Exception:
        return 0


def _contest_properties_map(contest_id: int) -> dict[str, str]:
    rows = config.db.fetch_all(
        "SELECT key,value_json FROM contest_properties WHERE contest_id=?",
        [int(contest_id)],
    )
    result: dict[str, str] = {}
    for row in rows:
        key = str(row["key"] or "").strip()
        if not key:
            continue
        raw_json = str(row["value_json"] or "").strip()
        value_text = ""
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                if parsed is None:
                    value_text = ""
                elif isinstance(parsed, str):
                    value_text = parsed
                else:
                    value_text = str(parsed)
            except Exception:
                value_text = raw_json
        result[key] = value_text
    return result


def _upsert_contest_property(contest_id: int, actor_user_id: int, key: str, value: str) -> None:
    config.db.execute(
        """
        INSERT INTO contest_properties(contest_id,key,value_json,updated_at,updated_by_user_id)
        VALUES(?,?,?,?,?)
        ON CONFLICT(contest_id,key) DO UPDATE SET
            value_json=excluded.value_json,
            updated_at=excluded.updated_at,
            updated_by_user_id=excluded.updated_by_user_id
        """,
        [int(contest_id), str(key), json.dumps(str(value or "")), now_iso(), int(actor_user_id)],
    )


def _contest_nav(contest_slug: str, user: str, active: str) -> list[dict[str, str]]:
    base = f"/contests/{contest_slug}/{user}"
    return [
        {"key": "overview", "label": "Overview", "href": f"{base}/overview", "active": "1" if active == "overview" else "0"},
        {"key": "problems", "label": "Problems", "href": f"{base}/problems", "active": "1" if active == "problems" else "0"},
        {"key": "properties", "label": "Properties", "href": f"{base}/properties", "active": "1" if active == "properties" else "0"},
        {"key": "access", "label": "Access", "href": f"{base}/access", "active": "1" if active == "access" else "0"},
        {"key": "packages", "label": "Packages", "href": f"{base}/packages", "active": "1" if active == "packages" else "0"},
    ]


def _contest_ctx(contest_slug: str, user: str, active_page: str) -> dict:
    gctx = _global_user_ctx(user)
    safe_slug = _normalize_contest_slug_required(contest_slug)
    contest_row = config.db.fetch_one(
        "SELECT id,slug,title,owner_user_id,created_at FROM contests WHERE slug=?",
        [safe_slug],
    )
    if contest_row is None:
        raise HTTPException(status_code=404, detail="contest not found")
    access = _contest_access_context(int(contest_row["id"]), int(gctx["user"]["id"]))
    if not access.get("can_read"):
        raise HTTPException(status_code=403, detail=str(access.get("read_block_reason") or "contest access required"))
    return {
        "user": gctx["user"],
        "contest": {
            "id": int(contest_row["id"]),
            "slug": str(contest_row["slug"]),
            "title": str(contest_row["title"]),
            "owner_user_id": int(contest_row["owner_user_id"]),
            "created_at": contest_row["created_at"],
        },
        "access": access,
        "active_main": "contests",
        "contest_nav": _contest_nav(str(contest_row["slug"]), str(gctx["user"]["username"]), active_page),
    }


def _contest_problem_rows(contest_id: int, username: str, user_id: int) -> list[dict]:
    rows = config.db.fetch_all(
        """
        SELECT cp.id AS contest_problem_id,cp.idx,cp.problem_id,cp.created_at,p.slug AS problem_slug,p.name AS problem_name
        FROM contest_problems cp
        JOIN problems p ON p.id=cp.problem_id
        WHERE cp.contest_id=?
        ORDER BY cp.idx COLLATE NOCASE ASC, cp.id ASC
        """,
        [int(contest_id)],
    )
    result: list[dict] = []
    for row in rows:
        problem_id = int(row["problem_id"])
        problem_slug = str(row["problem_slug"])
        problem_name = str(row["problem_name"])
        problem_access = _workspace_access_context(problem_id, int(user_id))
        can_problem_write = bool(problem_access.get("can_write"))
        revision_display = "unavailable"
        revision_warn = False
        dirty = False
        tl_ms = int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"])
        ml_mb = int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"])
        mode = str(_C.GENERAL_CONFIG_DEFAULTS["mode"])
        if bool(problem_access.get("can_read")):
            try:
                workspace = Path(config.workspace_service.ensure_workspace(problem_slug, username, refresh_status=True))
                ws_ctx = config.workspace_service.workspace_context(problem_slug, username, include_recent=False)
                branch = str(ws_ctx["workspace"].get("branch") or "main").strip() or "main"
                revision = _workspace_revision_info(workspace, branch)
                revision_display = str(revision.get("display") or "unknown")
                revision_warn = bool(revision.get("highlight"))
                dirty = bool(ws_ctx["workspace"].get("dirty"))
                _payload, general_cfg, _cfg_path = _read_problem_config(workspace)
                tl_ms = _coerce_int(
                    general_cfg.get("time_limit_ms"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
                    _C.GENERAL_TIME_LIMIT_MIN_MS,
                    _C.GENERAL_TIME_LIMIT_MAX_MS,
                )
                ml_mb = _coerce_int(
                    general_cfg.get("memory_limit_mb"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
                    _C.GENERAL_MEMORY_LIMIT_MIN_MB,
                    _C.GENERAL_MEMORY_LIMIT_MAX_MB,
                )
                mode = _normalize_problem_mode(general_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
            except Exception:
                revision_display = "unavailable"
        else:
            revision_display = "no problem access"
            revision_warn = True
        result.append(
            {
                "contest_problem_id": int(row["contest_problem_id"]),
                "idx": str(row["idx"]),
                "problem_id": problem_id,
                "problem_slug": problem_slug,
                "problem_name": problem_name,
                "time_limit_ms": tl_ms,
                "memory_limit_mb": ml_mb,
                "mode": mode,
                "revision_display": revision_display,
                "revision_warn": revision_warn,
                "dirty": dirty,
                "can_problem_write": can_problem_write,
                "created_at": row["created_at"],
            }
        )
    return result


def _contest_available_problem_rows(contest_id: int, user_id: int, query: str) -> list[dict]:
    cap = max(1, min(500, int(_C.API_PROBLEMS_LIST_LIMIT)))
    rows = config.db.fetch_all(
        """
        SELECT p.id,p.slug,p.name,a.role
        FROM repo_acl a
        JOIN problems p ON p.id=a.problem_id
        WHERE a.user_id=?
          AND p.id NOT IN (
              SELECT cp.problem_id
              FROM contest_problems cp
              WHERE cp.contest_id=?
          )
        ORDER BY p.slug ASC
        LIMIT ?
        """,
        [int(user_id), int(contest_id), cap],
    )
    q = str(query or "").strip().lower()
    result: list[dict] = []
    for row in rows:
        slug = str(row["slug"])
        name = str(row["name"])
        hay = f"{slug} {name}".lower()
        if q and q not in hay:
            continue
        result.append(
            {
                "problem_id": int(row["id"]),
                "problem_slug": slug,
                "problem_name": name,
                "role": _normalize_contest_role(row["role"]),
            }
        )
    return result


def _next_contest_problem_idx(contest_id: int) -> str:
    rows = config.db.fetch_all(
        "SELECT idx FROM contest_problems WHERE contest_id=?",
        [int(contest_id)],
    )
    used = {str(row["idx"] or "").strip().upper() for row in rows if str(row["idx"] or "").strip()}
    seq = 1
    while seq < 100000:
        token = _contest_idx_label(seq)
        if token not in used:
            return token
        seq += 1
    raise RuntimeError("unable to allocate contest problem index")


def _contest_problem_slug_file_token(problem_slug: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(problem_slug or "").strip()).strip("-")
    return token or "problem"


def _create_contest_job(
    contest_id: int,
    actor_user_id: int,
    job_type: str,
    status: str,
    summary: dict,
    *,
    finished_at: str | None = None,
) -> str:
    job_id = f"cj-{secrets.token_hex(6)}"
    now = now_iso()
    safe_status = str(status or "").strip().lower() or "failed"
    safe_finished_at = finished_at
    if safe_finished_at is None and safe_status not in {"running", "queued"}:
        safe_finished_at = now
    config.db.execute(
        """
        INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,summary_json,created_at,finished_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        [
            job_id,
            int(contest_id),
            int(actor_user_id),
            str(job_type or "").strip(),
            safe_status,
            json.dumps(summary, ensure_ascii=False),
            now,
            safe_finished_at,
        ],
    )
    return job_id


def _update_contest_job(
    contest_id: int,
    job_id: str,
    status: str,
    summary: dict,
    *,
    finished: bool = True,
) -> None:
    safe_job_id = str(job_id or "").strip()
    if not safe_job_id:
        return
    config.db.execute(
        """
        UPDATE contest_jobs
        SET status=?,summary_json=?,finished_at=?
        WHERE id=? AND contest_id=?
        """,
        [
            str(status or "").strip().lower() or "failed",
            json.dumps(summary, ensure_ascii=False),
            now_iso() if bool(finished) else None,
            safe_job_id,
            int(contest_id),
        ],
    )


def _load_contest_job(contest_id: int, job_id: str) -> dict | None:
    safe_job_id = str(job_id or "").strip()
    if not safe_job_id:
        return None
    row = config.db.fetch_one(
        """
        SELECT id,job_type,status,summary_json,created_at,finished_at
        FROM contest_jobs
        WHERE id=? AND contest_id=?
        """,
        [safe_job_id, int(contest_id)],
    )
    if row is None:
        return None
    summary: dict = {}
    raw = str(row["summary_json"] or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                summary = parsed
        except Exception:
            summary = {}
    return {
        "id": str(row["id"]),
        "job_type": str(row["job_type"] or ""),
        "status": str(row["status"] or ""),
        "summary": summary,
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def _contest_running_job(contest_id: int, job_type: str) -> str:
    row = config.db.fetch_one(
        """
        SELECT id
        FROM contest_jobs
        WHERE contest_id=? AND job_type=? AND status='running'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [int(contest_id), str(job_type or "").strip()],
    )
    if row is None:
        return ""
    return str(row["id"] or "").strip()


def _contest_artifacts_base() -> Path:
    base = (config.settings.artifacts_root / _CONTEST_ARTIFACTS_BUCKET).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _contest_job_root(contest_slug: str, job_id: str) -> Path:
    safe_slug = _normalize_contest_slug_required(contest_slug)
    safe_job_id = str(job_id or "").strip()
    if not is_canonical_artifact_id(safe_job_id):
        raise ValueError("invalid contest job id")
    base = _contest_artifacts_base()
    root = (base / safe_slug / safe_job_id).resolve()
    if base not in root.parents and base != root:
        raise ValueError("invalid contest artifact path")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _record_contest_artifact(
    *,
    contest_id: int,
    job_id: str,
    artifact_type: str,
    filename: str,
    artifact_path: Path,
) -> str:
    safe_filename = Path(str(filename or "").strip() or artifact_path.name).name
    resolved = artifact_path.resolve()
    base = _contest_artifacts_base()
    if base not in resolved.parents:
        raise ValueError("invalid contest artifact path")
    if not resolved.exists() or not resolved.is_file() or resolved.is_symlink():
        raise ValueError("contest artifact file not found")
    artifact_id = f"ca-{secrets.token_hex(6)}"
    config.db.execute(
        """
        INSERT INTO contest_artifacts(id,contest_id,job_id,artifact_type,filename,artifact_path,sha256,size_bytes,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        [
            artifact_id,
            int(contest_id),
            str(job_id or "").strip(),
            str(artifact_type or "").strip(),
            safe_filename,
            str(resolved),
            sha256_file(resolved),
            int(resolved.stat().st_size),
            now_iso(),
        ],
    )
    return artifact_id


def _contest_problem_entries(contest_id: int) -> list[dict[str, object]]:
    rows = config.db.fetch_all(
        """
        SELECT cp.idx,cp.problem_id,p.slug AS problem_slug,p.name AS problem_name
        FROM contest_problems cp
        JOIN problems p ON p.id=cp.problem_id
        WHERE cp.contest_id=?
        ORDER BY cp.idx COLLATE NOCASE ASC, cp.id ASC
        """,
        [int(contest_id)],
    )
    result: list[dict[str, object]] = []
    for row in rows:
        result.append(
            {
                "idx": str(row["idx"] or ""),
                "problem_id": int(row["problem_id"]),
                "problem_slug": str(row["problem_slug"] or ""),
                "problem_name": str(row["problem_name"] or ""),
            }
        )
    return result


def _ensure_zip_bundle(job_root: Path, bundle_name: str, source_dir: Path) -> Path:
    safe_name = Path(str(bundle_name or "").strip() or "contest-bundle").stem
    if not safe_name:
        safe_name = "contest-bundle"
    target_base = job_root / safe_name
    out = Path(shutil.make_archive(str(target_base), "zip", root_dir=source_dir, base_dir="."))
    return out.resolve()


def _contest_redirect(contest_slug: str, user: str, page: str, *, query: str = "", message: str = ""):
    target = f"/contests/{contest_slug}/{user}/{page}"
    if query:
        target += f"?{query}"
    return _redirect_response(target, status_code=303, message=message)


def contest_overview_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "overview")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    rows = _contest_problem_rows(contest_id, str(ctx["user"]["username"]), user_id)
    members_row = config.db.fetch_one(
        "SELECT COUNT(*) AS c FROM contest_members WHERE contest_id=?",
        [contest_id],
    )
    member_count = int(members_row["c"] or 0) if members_row is not None else 0
    latest_job = config.db.fetch_one(
        """
        SELECT id,job_type,status,created_at,finished_at
        FROM contest_jobs
        WHERE contest_id=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [contest_id],
    )
    props = _contest_properties_map(contest_id)
    return _template_response(
        request,
        "contest_overview.html",
        {
            "ctx": ctx,
            "problem_rows": rows,
            "problem_count": len(rows),
            "member_count": member_count,
            "owner_count": _contest_owner_count(contest_id),
            "latest_job": dict(latest_job) if latest_job is not None else None,
            "contest_properties": props,
        },
    )


def contest_problems_page(request: Request, contest: str, user: str, q: str = "", job_id: str = ""):
    ctx = _contest_ctx(contest, user, "problems")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    rows = _contest_problem_rows(contest_id, str(ctx["user"]["username"]), user_id)
    query = str(q or "").strip()
    available_rows = _contest_available_problem_rows(contest_id, user_id, query)
    latest_job = _load_contest_job(contest_id, job_id)
    return _template_response(
        request,
        "contest_problems.html",
        {
            "ctx": ctx,
            "query": query,
            "problem_rows": rows,
            "available_rows": available_rows,
            "latest_job": latest_job,
        },
    )


def contest_problems_add(contest: str, user: str, problem_slugs: list[str] = Form([]), q: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    safe_slugs = _dedupe_preserve([_form_text(item) for item in list(problem_slugs or [])])
    if not safe_slugs:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", query=f"q={quote_plus(str(q or ''))}" if str(q or "").strip() else "", message="select at least one problem to add")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    added = 0
    failed: list[str] = []
    for slug in safe_slugs:
        problem_row = config.db.fetch_one("SELECT id,slug FROM problems WHERE slug=?", [slug])
        if problem_row is None:
            failed.append(f"{slug}: problem not found")
            continue
        problem_id = int(problem_row["id"])
        problem_access = _workspace_access_context(problem_id, user_id)
        if not bool(problem_access.get("can_read")):
            failed.append(f"{slug}: no access to problem")
            continue
        existing = config.db.fetch_one(
            "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, problem_id],
        )
        if existing is not None:
            failed.append(f"{slug}: already in contest")
            continue
        try:
            idx = _next_contest_problem_idx(contest_id)
            config.db.execute(
                """
                INSERT INTO contest_problems(contest_id,idx,problem_id,added_by_user_id,created_at)
                VALUES(?,?,?,?,?)
                """,
                [contest_id, idx, problem_id, user_id, now_iso()],
            )
            added += 1
        except Exception as exc:
            failed.append(f"{slug}: {exc}")
    _audit(
        int(ctx["user"]["id"]),
        None,
        "contest.problems.add",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "added_count": added,
            "failed_count": len(failed),
            "failed": failed[:20],
        },
    )
    msg = f"added {added} problem(s)"
    if failed:
        msg += f"; failed {len(failed)}"
    query_parts: list[str] = []
    if str(q or "").strip():
        query_parts.append(f"q={quote_plus(str(q or '').strip())}")
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", query="&".join(query_parts), message=msg)


def contest_problems_remove(contest: str, user: str, problem_id: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    msg = "problem removed"
    try:
        pid = int(str(problem_id or "").strip())
    except Exception:
        pid = 0
    if pid <= 0:
        msg = "invalid problem id"
    else:
        config.db.execute(
            "DELETE FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, pid],
        )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message=msg)


def contest_problems_remove_selected(contest: str, user: str, selected_problem_ids: list[str] = Form([])):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    ids: list[int] = []
    for raw in list(selected_problem_ids or []):
        try:
            value = int(str(raw or "").strip())
        except Exception:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="select at least one problem to remove")
    contest_id = int(ctx["contest"]["id"])
    removed = 0
    for pid in ids:
        before = config.db.fetch_one(
            "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, pid],
        )
        config.db.execute(
            "DELETE FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, pid],
        )
        if before is not None:
            removed += 1
    msg = f"removed {removed} problem(s)"
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message=msg)


def contest_problems_reorder(
    contest: str,
    user: str,
    contest_problem_ids: list[str] = Form([]),
    contest_problem_indices: list[str] = Form([]),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    ids_raw = list(contest_problem_ids or [])
    idx_raw = list(contest_problem_indices or [])
    if not ids_raw or len(ids_raw) != len(idx_raw):
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="invalid reorder payload")
    pairs: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    seen_idx: set[str] = set()
    for i, raw_id in enumerate(ids_raw):
        try:
            cp_id = int(str(raw_id or "").strip())
        except Exception:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="invalid contest problem id")
        if cp_id <= 0 or cp_id in seen_ids:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="invalid contest problem id")
        idx = _normalize_contest_problem_idx_required(idx_raw[i])
        if idx in seen_idx:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="duplicate problem index")
        seen_ids.add(cp_id)
        seen_idx.add(idx)
        pairs.append((cp_id, idx))
    contest_id = int(ctx["contest"]["id"])

    def _tx(conn) -> bool:
        for cp_id, _idx in pairs:
            row = conn.execute(
                "SELECT id FROM contest_problems WHERE id=? AND contest_id=?",
                [cp_id, contest_id],
            ).fetchone()
            if row is None:
                return False
        for cp_id, idx in pairs:
            temp_idx = f"TMP-{cp_id}-{secrets.token_hex(3)}"
            conn.execute(
                "UPDATE contest_problems SET idx=? WHERE id=? AND contest_id=?",
                [temp_idx, cp_id, contest_id],
            )
        for cp_id, idx in pairs:
            conn.execute(
                "UPDATE contest_problems SET idx=? WHERE id=? AND contest_id=?",
                [idx, cp_id, contest_id],
            )
        return True

    updated = bool(config.db.write_transaction(_tx))
    if not updated:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="contest problem not found")
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="problem order saved")


def contest_problems_renumber(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    rows = config.db.fetch_all(
        "SELECT id FROM contest_problems WHERE contest_id=? ORDER BY idx COLLATE NOCASE ASC, id ASC",
        [contest_id],
    )

    def _tx(conn) -> None:
        seq = 1
        for row in rows:
            conn.execute(
                "UPDATE contest_problems SET idx=? WHERE id=? AND contest_id=?",
                [_contest_idx_label(seq), int(row["id"]), contest_id],
            )
            seq += 1

    config.db.write_transaction(_tx)
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="problem indices renumbered")


def _problem_general_payload_map(
    problem_ids: list[str],
    problem_names: list[str],
    time_limit_ms_values: list[str],
    memory_limit_mb_values: list[str],
) -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    for index, raw_pid in enumerate(list(problem_ids or [])):
        try:
            pid = int(str(raw_pid or "").strip())
        except Exception:
            continue
        if pid <= 0:
            continue
        name_value = str(problem_names[index] if index < len(problem_names) else "").strip()
        tl_value = str(time_limit_ms_values[index] if index < len(time_limit_ms_values) else "").strip()
        ml_value = str(memory_limit_mb_values[index] if index < len(memory_limit_mb_values) else "").strip()
        result[pid] = {"name": name_value, "time_limit_ms": tl_value, "memory_limit_mb": ml_value}
    return result


def _run_problem_general_update(
    *,
    contest_slug: str,
    actor_username: str,
    actor_user_id: int,
    problem_id: int,
    problem_slug: str,
    requested_name: str,
    requested_time_limit_ms: str,
    requested_memory_limit_mb: str,
) -> dict[str, object]:
    requested: dict[str, object] = {
        "name": str(requested_name or "").strip(),
        "time_limit_ms": str(requested_time_limit_ms or "").strip(),
        "memory_limit_mb": str(requested_memory_limit_mb or "").strip(),
    }
    result: dict[str, object] = {
        "problem_id": int(problem_id),
        "problem_slug": str(problem_slug),
        "requested": requested,
        "status": "failed",
        "commit_id": "",
        "error": "",
    }
    problem_access = _workspace_access_context(int(problem_id), int(actor_user_id))
    if not bool(problem_access.get("can_write")):
        result["error"] = "write access to problem is required"
        return result
    try:
        workspace = Path(config.workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True))
        safe_name = _normalize_problem_name_required(str(requested.get("name") or ""))
        safe_tl = _coerce_int(
            requested.get("time_limit_ms"),
            int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            _C.GENERAL_TIME_LIMIT_MIN_MS,
            _C.GENERAL_TIME_LIMIT_MAX_MS,
        )
        safe_ml = _coerce_int(
            requested.get("memory_limit_mb"),
            int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
            _C.GENERAL_MEMORY_LIMIT_MIN_MB,
            _C.GENERAL_MEMORY_LIMIT_MAX_MB,
        )
        with config.workspace_service.workspace_lock(workspace):
            has_head = run_cmd(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"]).returncode == 0
            before = config.git_service.status_change_summary(workspace, limit=1)
            if int(before.get("total") or 0) > 0 and has_head:
                raise RuntimeError("workspace has uncommitted changes")
            payload, general_cfg, cfg_path = _read_problem_config(workspace)
            safe_mode = _normalize_problem_mode(general_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
            payload.pop("interactive", None)
            payload.update(
                {
                    "time_limit_ms": safe_tl,
                    "memory_limit_mb": safe_ml,
                    "mode": safe_mode,
                }
            )
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config.workspace_service.set_problem_name(problem_slug, safe_name)
            after = config.git_service.status_change_summary(workspace, limit=1)
            if int(after.get("total") or 0) <= 0:
                result["status"] = "skipped"
                return result
            commit_msg = f"contest {contest_slug}: bulk update name/TL/ML"
            commit_id = config.git_service.commit(
                workspace,
                commit_msg,
                actor_username,
                f"{actor_username}@polygonlike.local",
            )
            try:
                config.git_service.push(workspace, "main")
            except Exception as exc:
                try:
                    config.git_service.rollback_last_commit(workspace, expected_head=commit_id)
                except Exception as rollback_exc:
                    raise RuntimeError(f"push failed: {exc}; rollback failed: {rollback_exc}") from exc
                raise RuntimeError(f"push failed: {exc}; commit rolled back") from exc
            result["status"] = "success"
            result["commit_id"] = str(commit_id)
        try:
            config.workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True)
        except Exception:
            pass
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def contest_problems_change_general(
    contest: str,
    user: str,
    selected_problem_ids: list[str] = Form([]),
    problem_ids: list[str] = Form([]),
    problem_names: list[str] = Form([]),
    time_limit_ms_values: list[str] = Form([]),
    memory_limit_mb_values: list[str] = Form([]),
    retry_job_id: str = Form(""),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    selected_ids: list[int] = []
    for raw in list(selected_problem_ids or []):
        try:
            value = int(str(raw or "").strip())
        except Exception:
            continue
        if value > 0 and value not in selected_ids:
            selected_ids.append(value)
    requested_map = _problem_general_payload_map(
        list(problem_ids or []),
        list(problem_names or []),
        list(time_limit_ms_values or []),
        list(memory_limit_mb_values or []),
    )
    safe_retry_job_id = str(retry_job_id or "").strip()
    if safe_retry_job_id and not selected_ids:
        retry_job = _load_contest_job(contest_id, safe_retry_job_id)
        if retry_job is None:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="retry job not found")
        if str(retry_job.get("job_type") or "") != "change-general":
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="retry job type is invalid")
        summary = retry_job.get("summary")
        if isinstance(summary, dict):
            results = summary.get("results")
            if isinstance(results, list):
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("status") or "").strip() != "failed":
                        continue
                    try:
                        pid = int(item.get("problem_id") or 0)
                    except Exception:
                        continue
                    if pid <= 0:
                        continue
                    req = item.get("requested")
                    if isinstance(req, dict):
                        requested_map[pid] = {
                            "name": str(req.get("name") or "").strip(),
                            "time_limit_ms": str(req.get("time_limit_ms") or "").strip(),
                            "memory_limit_mb": str(req.get("memory_limit_mb") or "").strip(),
                        }
                    if pid not in selected_ids:
                        selected_ids.append(pid)
    if not selected_ids:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="select at least one problem to update")
    placeholders = ",".join(("?" for _ in selected_ids))
    rows = config.db.fetch_all(
        f"""
        SELECT cp.idx,cp.problem_id,p.slug,p.name
        FROM contest_problems cp
        JOIN problems p ON p.id=cp.problem_id
        WHERE cp.contest_id=?
          AND cp.problem_id IN ({placeholders})
        ORDER BY cp.idx COLLATE NOCASE ASC, cp.id ASC
        """,
        [contest_id, *selected_ids],
    )
    if not rows:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="selected problems are not part of this contest")
    results: list[dict[str, object]] = []
    for row in rows:
        pid = int(row["problem_id"])
        defaults = {
            "name": str(row["name"] or "").strip(),
            "time_limit_ms": str(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            "memory_limit_mb": str(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
        }
        requested = requested_map.get(pid, defaults)
        result = _run_problem_general_update(
            contest_slug=str(ctx["contest"]["slug"]),
            actor_username=str(ctx["user"]["username"]),
            actor_user_id=actor_user_id,
            problem_id=pid,
            problem_slug=str(row["slug"]),
            requested_name=str(requested.get("name") or ""),
            requested_time_limit_ms=str(requested.get("time_limit_ms") or ""),
            requested_memory_limit_mb=str(requested.get("memory_limit_mb") or ""),
        )
        results.append(result)
    success_count = sum((1 for row in results if str(row.get("status") or "") == "success"))
    failed_count = sum((1 for row in results if str(row.get("status") or "") == "failed"))
    skipped_count = sum((1 for row in results if str(row.get("status") or "") == "skipped"))
    summary = {
        "contest_slug": str(ctx["contest"]["slug"]),
        "job_type": "change-general",
        "results": results,
        "totals": {
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "skipped": skipped_count,
        },
    }
    job_status = "ok" if failed_count == 0 else "failed"
    job_id = _create_contest_job(contest_id, actor_user_id, "change-general", job_status, summary)
    _audit(
        actor_user_id,
        None,
        "contest.problems.change_general",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "job_id": job_id,
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "skipped": skipped_count,
        },
    )
    msg = f"change names/TL/ML finished: {success_count} success, {failed_count} failed, {skipped_count} skipped"
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", query=f"job_id={quote_plus(job_id)}", message=msg)


def contest_properties_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "properties")
    contest_id = int(ctx["contest"]["id"])
    props = _contest_properties_map(contest_id)
    source_mode = str(props.get(_CONTEST_PROPERTY_SOURCE_MODE) or "latest_committed").strip()
    if source_mode not in _CONTEST_SOURCE_MODE_VALUES:
        source_mode = "latest_committed"
    return _template_response(
        request,
        "contest_properties.html",
        {
            "ctx": ctx,
            "location": str(props.get(_CONTEST_PROPERTY_LOCATION) or ""),
            "date_text": str(props.get(_CONTEST_PROPERTY_DATE) or ""),
            "source_mode": source_mode,
        },
    )


def contest_properties_save(
    contest: str,
    user: str,
    title: str = Form(""),
    location: str = Form(""),
    date_text: str = Form(""),
    source_mode: str = Form("latest_committed"),
):
    ctx = _contest_ctx(contest, user, "properties")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    current_title = str(ctx["contest"]["title"] or "").strip()
    safe_title = _normalize_contest_title_required(str(title or "").strip() or current_title)
    safe_location = str(location or "").strip()
    safe_date = str(date_text or "").strip()
    safe_source_mode = str(source_mode or "").strip().lower()
    if safe_source_mode not in _CONTEST_SOURCE_MODE_VALUES:
        safe_source_mode = "latest_committed"
    config.db.execute(
        "UPDATE contests SET title=? WHERE id=?",
        [safe_title, contest_id],
    )
    _upsert_contest_property(contest_id, actor_user_id, _CONTEST_PROPERTY_LOCATION, safe_location)
    _upsert_contest_property(contest_id, actor_user_id, _CONTEST_PROPERTY_DATE, safe_date)
    _upsert_contest_property(contest_id, actor_user_id, _CONTEST_PROPERTY_SOURCE_MODE, safe_source_mode)
    _audit(
        actor_user_id,
        None,
        "contest.properties.save",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "title": safe_title,
            "location": safe_location,
            "date": safe_date,
            "source_mode": safe_source_mode,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "properties", message="contest properties saved")


def contest_access_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "access")
    contest_id = int(ctx["contest"]["id"])
    rows = config.db.fetch_all(
        """
        SELECT u.username,m.role,m.created_at
        FROM contest_members m
        JOIN users u ON u.id=m.user_id
        WHERE m.contest_id=?
        ORDER BY
            CASE m.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
            u.username ASC
        """,
        [contest_id],
    )
    entries: list[dict[str, object]] = []
    for row in rows:
        entries.append(
            {
                "username": str(row["username"]),
                "role": _normalize_contest_role(row["role"]),
                "created_at": row["created_at"],
            }
        )
    return _template_response(
        request,
        "contest_access.html",
        {
            "ctx": ctx,
            "entries": entries,
            "owner_count": _contest_owner_count(contest_id),
        },
    )


def contest_access_grant(contest: str, user: str, target_user: str = Form(...), role: str = Form("read")):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("manage_block_reason") or "owner access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = str(target_user or "").strip()
    safe_role = _normalize_contest_member_role_required(role)
    user_row = config.db.fetch_one("SELECT id FROM users WHERE username=?", [safe_target])
    if user_row is None:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"user {safe_target} not found; ask them to register first")
    target_user_id = int(user_row["id"])
    config.db.execute(
        """
        INSERT INTO contest_members(contest_id,user_id,role,created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(contest_id,user_id) DO UPDATE SET role=excluded.role
        """,
        [contest_id, target_user_id, safe_role, now_iso()],
    )
    _audit(
        actor_user_id,
        None,
        "contest.access.grant",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "target_user": safe_target,
            "role": safe_role,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"granted {safe_role} to {safe_target}")


def contest_access_revoke(contest: str, user: str, target_user: str = Form(...)):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("manage_block_reason") or "owner access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = str(target_user or "").strip()
    target_row = config.db.fetch_one(
        """
        SELECT m.role,u.id AS user_id
        FROM contest_members m
        JOIN users u ON u.id=m.user_id
        WHERE m.contest_id=? AND u.username=?
        """,
        [contest_id, safe_target],
    )
    if target_row is None:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"{safe_target} is not a member")
    target_role = _normalize_contest_role(target_row["role"])
    if target_role == "owner" and _contest_owner_count(contest_id) <= 1:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message="cannot remove the last owner")
    config.db.execute(
        """
        DELETE FROM contest_members
        WHERE contest_id=?
          AND user_id=?
        """,
        [contest_id, int(target_row["user_id"])],
    )
    _audit(
        actor_user_id,
        None,
        "contest.access.revoke",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "target_user": safe_target,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"revoked access for {safe_target}")


def _finalize_contest_job_failure_if_running(
    *,
    contest_id: int,
    job_id: str,
    job_type: str,
    error_text: str,
) -> None:
    row = config.db.fetch_one(
        "SELECT status FROM contest_jobs WHERE id=? AND contest_id=?",
        [str(job_id or "").strip(), int(contest_id)],
    )
    if row is None:
        return
    current_status = str(row["status"] or "").strip().lower()
    if current_status != "running":
        return
    _update_contest_job(
        contest_id,
        str(job_id or "").strip(),
        "failed",
        {
            "job_type": str(job_type or "").strip(),
            "error": str(error_text or "").strip() or "worker failed",
        },
        finished=True,
    )


def _run_contest_preview_job_worker(
    *,
    contest_id: int,
    contest_slug: str,
    actor_user_id: int,
    actor_username: str,
    job_id: str,
) -> None:
    job_root = _contest_job_root(contest_slug, job_id)
    preview_dir = job_root / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    entries = _contest_problem_entries(contest_id)
    results: list[dict[str, object]] = []
    for entry in entries:
        problem_id = int(entry["problem_id"])
        idx = str(entry["idx"] or "")
        problem_slug = str(entry["problem_slug"] or "")
        item: dict[str, object] = {
            "idx": idx,
            "problem_id": problem_id,
            "problem_slug": problem_slug,
            "status": "failed",
            "preview_id": "",
            "output_pdf": "",
            "error": "",
        }
        access = _workspace_access_context(problem_id, actor_user_id)
        if not bool(access.get("can_read")):
            item["error"] = "read access to problem is required"
            results.append(item)
            continue
        try:
            preview_id = str(config.preview_service.compile_preview(problem_slug, actor_username) or "").strip()
            if not preview_id:
                raise RuntimeError("preview id missing")
            row = config.db.fetch_one(
                """
                SELECT status,summary_json
                FROM previews
                WHERE id=? AND problem_id=?
                """,
                [preview_id, problem_id],
            )
            if row is None:
                raise RuntimeError("preview metadata missing")
            preview_status = str(row["status"] or "").strip().lower()
            item["preview_id"] = preview_id
            if preview_status != "ok":
                error_text = "preview failed"
                raw_summary = str(row["summary_json"] or "").strip()
                if raw_summary:
                    try:
                        summary = json.loads(raw_summary)
                    except Exception:
                        summary = {}
                    if isinstance(summary, dict):
                        error_text = str(summary.get("error") or error_text)
                raise RuntimeError(error_text)
            source_pdf = (config.settings.artifacts_root / problem_slug / preview_id / "statement_preview" / "statement.pdf").resolve()
            problem_artifacts_root = (config.settings.artifacts_root / problem_slug).resolve()
            if problem_artifacts_root not in source_pdf.parents:
                raise RuntimeError("invalid preview artifact path")
            if not source_pdf.exists() or not source_pdf.is_file() or source_pdf.is_symlink():
                raise RuntimeError("preview pdf is missing")
            file_token = _contest_problem_slug_file_token(problem_slug)
            output_name = f"{idx}-{file_token}.pdf" if idx else f"{file_token}.pdf"
            target_pdf = (preview_dir / output_name).resolve()
            shutil.copy2(source_pdf, target_pdf)
            item["output_pdf"] = f"preview/{output_name}"
            item["status"] = "success"
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
        results.append(item)
    success_count = sum((1 for row in results if str(row.get("status") or "") == "success"))
    failed_count = sum((1 for row in results if str(row.get("status") or "") != "success"))
    bundle_root = job_root / "bundle-preview"
    if bundle_root.exists():
        shutil.rmtree(bundle_root, ignore_errors=True)
    bundle_root.mkdir(parents=True, exist_ok=True)
    if preview_dir.exists() and any(preview_dir.iterdir()):
        shutil.copytree(preview_dir, bundle_root / "preview", dirs_exist_ok=True)
    summary: dict[str, object] = {
        "job_type": _CONTEST_JOB_TYPE_PREVIEW,
        "contest_slug": contest_slug,
        "results": results,
        "totals": {
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
        },
    }
    (bundle_root / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact_id = ""
    artifact_filename = ""
    if success_count > 0:
        archive_path = _ensure_zip_bundle(job_root, f"{contest_slug}-preview-{job_id}", bundle_root)
        artifact_filename = archive_path.name
        artifact_id = _record_contest_artifact(
            contest_id=contest_id,
            job_id=job_id,
            artifact_type="preview-bundle",
            filename=archive_path.name,
            artifact_path=archive_path,
        )
    summary["artifact_id"] = artifact_id
    summary["filename"] = artifact_filename
    final_status = "ok" if failed_count == 0 and success_count > 0 else "failed"
    _update_contest_job(contest_id, job_id, final_status, summary, finished=True)
    _audit(
        actor_user_id,
        None,
        "contest.packages.preview",
        {
            "contest_id": contest_id,
            "contest_slug": contest_slug,
            "job_id": job_id,
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "artifact_id": artifact_id,
        },
    )


def _run_contest_package_job_worker(
    *,
    contest_id: int,
    contest_slug: str,
    actor_user_id: int,
    actor_username: str,
    job_id: str,
) -> None:
    job_root = _contest_job_root(contest_slug, job_id)
    packages_dir = job_root / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    entries = _contest_problem_entries(contest_id)
    results: list[dict[str, object]] = []
    for entry in entries:
        problem_id = int(entry["problem_id"])
        idx = str(entry["idx"] or "")
        problem_slug = str(entry["problem_slug"] or "")
        item: dict[str, object] = {
            "idx": idx,
            "problem_id": problem_id,
            "problem_slug": problem_slug,
            "status": "failed",
            "head_commit": "",
            "build_id": "",
            "package_file": "",
            "error": "",
        }
        access = _workspace_access_context(problem_id, actor_user_id)
        if not bool(access.get("can_read")):
            item["error"] = "read access to problem is required"
            results.append(item)
            continue
        try:
            config.workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True)
            ws_ctx = config.workspace_service.workspace_context(problem_slug, actor_username, include_recent=False)
            workspace_id = int(ws_ctx["workspace"]["id"])
            head_commit = str(ws_ctx["workspace"].get("head_commit") or "").strip()
            item["head_commit"] = head_commit
            if not head_commit:
                raise RuntimeError("no committed revision; commit changes first")
            committed_build = _latest_workspace_committed_build(problem_id, workspace_id, head_commit, ok_only=True)
            build_id = str(committed_build["id"] or "").strip() if committed_build is not None else ""
            if not build_id:
                build_id = str(
                    config.build_service.run_build(
                        problem_slug,
                        actor_username,
                        commit=head_commit,
                        ref=head_commit,
                    )
                    or ""
                ).strip()
            if not build_id:
                raise RuntimeError("failed to resolve build id")
            build_row = config.db.fetch_one(
                """
                SELECT status,source_commit,source_ref
                FROM builds
                WHERE id=? AND problem_id=? AND workspace_id=?
                """,
                [build_id, problem_id, workspace_id],
            )
            if build_row is None:
                raise RuntimeError(f"build metadata not found: {build_id}")
            build_status = str(build_row["status"] or "").strip().lower()
            source_commit = str(build_row["source_commit"] or "").strip()
            source_ref = str(build_row["source_ref"] or "").strip()
            if build_status != "ok":
                raise RuntimeError(f"build status is {build_status}")
            if source_commit != head_commit or source_ref != head_commit:
                raise RuntimeError("build is not from latest committed revision")
            export_path = Path(config.export_service.create_export(problem_slug, build_id, "icpc")).resolve()
            problem_artifacts_root = (config.settings.artifacts_root / problem_slug).resolve()
            if problem_artifacts_root not in export_path.parents:
                raise RuntimeError("invalid package artifact path")
            if not export_path.exists() or not export_path.is_file() or export_path.is_symlink():
                raise RuntimeError("package file missing")
            file_token = _contest_problem_slug_file_token(problem_slug)
            output_name = f"{idx}-{file_token}.zip" if idx else f"{file_token}.zip"
            target_package = (packages_dir / output_name).resolve()
            shutil.copy2(export_path, target_package)
            item["build_id"] = build_id
            item["package_file"] = f"packages/{output_name}"
            item["status"] = "success"
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
        results.append(item)
    success_count = sum((1 for row in results if str(row.get("status") or "") == "success"))
    failed_count = sum((1 for row in results if str(row.get("status") or "") != "success"))
    bundle_root = job_root / "bundle-package"
    if bundle_root.exists():
        shutil.rmtree(bundle_root, ignore_errors=True)
    bundle_root.mkdir(parents=True, exist_ok=True)
    if packages_dir.exists() and any(packages_dir.iterdir()):
        shutil.copytree(packages_dir, bundle_root / "packages", dirs_exist_ok=True)
    summary: dict[str, object] = {
        "job_type": _CONTEST_JOB_TYPE_PACKAGE,
        "contest_slug": contest_slug,
        "results": results,
        "totals": {
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
        },
    }
    (bundle_root / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact_id = ""
    artifact_filename = ""
    if success_count > 0:
        archive_path = _ensure_zip_bundle(job_root, f"{contest_slug}-packages-{job_id}", bundle_root)
        artifact_filename = archive_path.name
        artifact_id = _record_contest_artifact(
            contest_id=contest_id,
            job_id=job_id,
            artifact_type="package-bundle",
            filename=archive_path.name,
            artifact_path=archive_path,
        )
    summary["artifact_id"] = artifact_id
    summary["filename"] = artifact_filename
    final_status = "ok" if failed_count == 0 and success_count > 0 else "failed"
    _update_contest_job(contest_id, job_id, final_status, summary, finished=True)
    _audit(
        actor_user_id,
        None,
        "contest.packages.build",
        {
            "contest_id": contest_id,
            "contest_slug": contest_slug,
            "job_id": job_id,
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "artifact_id": artifact_id,
        },
    )


def _queue_contest_job(
    *,
    contest_id: int,
    contest_slug: str,
    actor_user_id: int,
    actor_username: str,
    job_type: str,
) -> tuple[str, bool, str]:
    active_id = _contest_running_job(contest_id, job_type)
    if active_id:
        return (active_id, False, "already_running")
    initial_summary = {
        "job_type": str(job_type or "").strip(),
        "contest_slug": str(contest_slug or "").strip(),
        "status": "running",
        "results": [],
    }
    job_id = _create_contest_job(
        contest_id,
        actor_user_id,
        str(job_type or "").strip(),
        "running",
        initial_summary,
        finished_at=None,
    )

    def _runner() -> None:
        try:
            if job_type == _CONTEST_JOB_TYPE_PREVIEW:
                _run_contest_preview_job_worker(
                    contest_id=contest_id,
                    contest_slug=contest_slug,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    job_id=job_id,
                )
                return
            if job_type == _CONTEST_JOB_TYPE_PACKAGE:
                _run_contest_package_job_worker(
                    contest_id=contest_id,
                    contest_slug=contest_slug,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    job_id=job_id,
                )
                return
            raise RuntimeError(f"unsupported contest job type: {job_type}")
        except Exception as exc:
            _finalize_contest_job_failure_if_running(
                contest_id=contest_id,
                job_id=job_id,
                job_type=job_type,
                error_text=str(exc),
            )
            raise

    _future, queued, submit_reason = config.worker_queue_service.submit(
        name=f"contest-{job_type}-{contest_id}",
        fn=_runner,
        queue_name=f"contest-{job_type}",
        backend=config.sandbox_backend.name,
        dedupe_key=f"contest:{contest_id}:{job_type}",
        job_type=f"contest-{job_type}",
    )
    if not queued:
        _update_contest_job(
            contest_id,
            job_id,
            "failed",
            {
                "job_type": str(job_type or "").strip(),
                "contest_slug": str(contest_slug or "").strip(),
                "error": f"queue rejected ({submit_reason})",
            },
            finished=True,
        )
    return (job_id, bool(queued), str(submit_reason or "").strip())


def contest_packages_preview_start(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    problem_count = int(problem_count_row["c"] or 0) if problem_count_row is not None else 0
    if problem_count <= 0:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", message="add at least one problem first")
    job_id, queued, reason = _queue_contest_job(
        contest_id=contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        actor_user_id=int(ctx["user"]["id"]),
        actor_username=str(ctx["user"]["username"]),
        job_type=_CONTEST_JOB_TYPE_PREVIEW,
    )
    if queued:
        message = "contest preview queued"
    elif reason == "already_running":
        message = f"contest preview already running ({job_id})"
    else:
        message = f"contest preview queue rejected ({reason})"
    _audit(
        int(ctx["user"]["id"]),
        None,
        "contest.packages.preview.start",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "job_id": job_id,
            "queued": bool(queued),
            "reason": reason,
        },
    )
    query = f"job_id={quote_plus(job_id)}" if job_id else ""
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", query=query, message=message)


def contest_packages_build_start(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    problem_count = int(problem_count_row["c"] or 0) if problem_count_row is not None else 0
    if problem_count <= 0:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", message="add at least one problem first")
    job_id, queued, reason = _queue_contest_job(
        contest_id=contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        actor_user_id=int(ctx["user"]["id"]),
        actor_username=str(ctx["user"]["username"]),
        job_type=_CONTEST_JOB_TYPE_PACKAGE,
    )
    if queued:
        message = "contest package build queued"
    elif reason == "already_running":
        message = f"contest package build already running ({job_id})"
    else:
        message = f"contest package build queue rejected ({reason})"
    _audit(
        int(ctx["user"]["id"]),
        None,
        "contest.packages.build.start",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "job_id": job_id,
            "queued": bool(queued),
            "reason": reason,
        },
    )
    query = f"job_id={quote_plus(job_id)}" if job_id else ""
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", query=query, message=message)


def contest_packages_job_status(contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    job = _load_contest_job(contest_id, str(job_id or "").strip())
    if job is None:
        return JSONResponse({"ok": False, "running": False, "job_id": str(job_id or "").strip(), "status": "missing"}, status_code=404)
    status = str(job.get("status") or "").strip().lower()
    return JSONResponse(
        {
            "ok": True,
            "running": status == "running",
            "job_id": str(job.get("id") or ""),
            "job_type": str(job.get("job_type") or ""),
            "status": status,
            "created_at": job.get("created_at"),
            "finished_at": job.get("finished_at"),
        }
    )


def contest_packages_artifact_download(contest: str, user: str, artifact_id: str):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    safe_artifact_id = str(artifact_id or "").strip()
    if not is_canonical_artifact_id(safe_artifact_id):
        raise HTTPException(status_code=404, detail="contest artifact not found")
    row = config.db.fetch_one(
        """
        SELECT id,filename,artifact_path
        FROM contest_artifacts
        WHERE contest_id=? AND id=?
        """,
        [contest_id, safe_artifact_id],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="contest artifact not found")
    file_path = Path(str(row["artifact_path"] or "")).resolve()
    base = _contest_artifacts_base()
    if base not in file_path.parents:
        raise HTTPException(status_code=404, detail="contest artifact not found")
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        raise HTTPException(status_code=404, detail="contest artifact file not found")
    download_name = Path(str(row["filename"] or "")).name.strip() or file_path.name
    return FileResponse(file_path, filename=download_name)


def contest_packages_page(request: Request, contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    requested_job_id = str(job_id or "").strip()
    artifact_rows = config.db.fetch_all(
        """
        SELECT id,job_id,artifact_type,filename,artifact_path,size_bytes,created_at
        FROM contest_artifacts
        WHERE contest_id=?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        [contest_id],
    )
    job_rows = config.db.fetch_all(
        """
        SELECT id,job_type,status,summary_json,created_at,finished_at
        FROM contest_jobs
        WHERE contest_id=?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        [contest_id],
    )
    display_job_rows: list[dict[str, object]] = []
    for row in job_rows:
        item = dict(row)
        summary: dict[str, object] = {}
        raw = str(item.get("summary_json") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                summary = parsed
        item["summary"] = summary
        display_job_rows.append(item)
    selected_job = _load_contest_job(contest_id, requested_job_id)
    if selected_job is None and display_job_rows:
        selected_job = _load_contest_job(contest_id, str(display_job_rows[0].get("id") or ""))
    base = _contest_artifacts_base()
    display_artifacts: list[dict[str, object]] = []
    for row in artifact_rows:
        item = dict(row)
        safe_id = str(item.get("id") or "").strip()
        safe_path = Path(str(item.get("artifact_path") or "")).resolve()
        downloadable = bool(
            safe_id
            and is_canonical_artifact_id(safe_id)
            and base in safe_path.parents
            and safe_path.exists()
            and safe_path.is_file()
            and (not safe_path.is_symlink())
        )
        item["downloadable"] = downloadable
        item["download_href"] = (
            f"/contests/{ctx['contest']['slug']}/{ctx['user']['username']}/packages/artifacts/{safe_id}"
            if downloadable
            else ""
        )
        display_artifacts.append(item)
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    problem_count = int(problem_count_row["c"] or 0) if problem_count_row is not None else 0
    return _template_response(
        request,
        "contest_packages.html",
        {
            "ctx": ctx,
            "artifact_rows": display_artifacts,
            "job_rows": display_job_rows,
            "selected_job": selected_job,
            "problem_count": problem_count,
            "requested_job_id": requested_job_id,
        },
    )
