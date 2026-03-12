from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path

from fastapi import HTTPException

from app.db import now_iso
from app.impl.auth.public import redirect_response
from app.impl.runtime.config import config
from .common import (
    _contest_idx_label,
    _contest_problem_slug_file_token,
)
from app.impl.workspace.public import (
    audit,
    coerce_int,
    global_user_ctx,
    latest_workspace_committed_build,
    normalize_contest_role,
    normalize_contest_slug_required,
    normalize_problem_mode,
    normalize_problem_name_required,
    read_problem_config,
    workspace_access_context,
    workspace_revision_info,
)
from app.service.platform.hashing import sha256_file
from app.service.platform.process import is_canonical_artifact_id, run_cmd

_C = config.constants

_CONTEST_PROPERTY_SOURCE_MODE = "source_mode"
_CONTEST_PROPERTY_LOCATION = "location"
_CONTEST_PROPERTY_DATE = "date"
_CONTEST_SOURCE_MODE_VALUES = {"latest_committed", "built_packages"}
_CONTEST_JOB_TYPE_PREVIEW = "preview"
_CONTEST_JOB_TYPE_PACKAGE = "package"
_CONTEST_ARTIFACTS_BUCKET = "__contests__"

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
    role = normalize_contest_role(row["role"])
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
    gctx = global_user_ctx(user)
    safe_slug = normalize_contest_slug_required(contest_slug)
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
        problem_access = workspace_access_context(problem_id, int(user_id))
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
                revision = workspace_revision_info(workspace, branch)
                revision_display = str(revision.get("display") or "unknown")
                revision_warn = bool(revision.get("highlight"))
                dirty = bool(ws_ctx["workspace"].get("dirty"))
                _payload, general_cfg, _cfg_path = read_problem_config(workspace)
                tl_ms = coerce_int(
                    general_cfg.get("time_limit_ms"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
                    _C.GENERAL_TIME_LIMIT_MIN_MS,
                    _C.GENERAL_TIME_LIMIT_MAX_MS,
                )
                ml_mb = coerce_int(
                    general_cfg.get("memory_limit_mb"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
                    _C.GENERAL_MEMORY_LIMIT_MIN_MB,
                    _C.GENERAL_MEMORY_LIMIT_MAX_MB,
                )
                mode = normalize_problem_mode(general_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
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
                "role": normalize_contest_role(row["role"]),
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
    safe_slug = normalize_contest_slug_required(contest_slug)
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
    return redirect_response(target, status_code=303, message=message)

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
    problem_access = workspace_access_context(int(problem_id), int(actor_user_id))
    if not bool(problem_access.get("can_write")):
        result["error"] = "write access to problem is required"
        return result
    try:
        workspace = Path(config.workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True))
        safe_name = normalize_problem_name_required(str(requested.get("name") or ""))
        safe_tl = coerce_int(
            requested.get("time_limit_ms"),
            int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            _C.GENERAL_TIME_LIMIT_MIN_MS,
            _C.GENERAL_TIME_LIMIT_MAX_MS,
        )
        safe_ml = coerce_int(
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
            payload, general_cfg, cfg_path = read_problem_config(workspace)
            safe_mode = normalize_problem_mode(general_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
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
        access = workspace_access_context(problem_id, actor_user_id)
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
    audit(
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
        access = workspace_access_context(problem_id, actor_user_id)
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
            committed_build = latest_workspace_committed_build(problem_id, workspace_id, head_commit, ok_only=True)
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
    audit(
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
        backend=config.invocation_backend_service.active_backend_name(),
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


