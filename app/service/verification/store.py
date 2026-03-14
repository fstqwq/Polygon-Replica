from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any

from app.db import DB, now_iso
from app.service.platform.fs.layout import FsManager

VERIFICATION_KIND_VERIFICATION = "verification"
VERIFICATION_KIND_SAMPLE = "sample"

_VERIFICATION_UPDATE_LOCKS_GUARD = threading.Lock()
_VERIFICATION_UPDATE_LOCKS: dict[str, threading.RLock] = {}


def verification_update_lock(verification_id: str) -> threading.RLock:
    safe_id = str(verification_id or "").strip()
    if not safe_id:
        raise RuntimeError("verification id is required")
    with _VERIFICATION_UPDATE_LOCKS_GUARD:
        lock = _VERIFICATION_UPDATE_LOCKS.get(safe_id)
        if lock is None:
            lock = threading.RLock()
            _VERIFICATION_UPDATE_LOCKS[safe_id] = lock
        return lock


def _normalize_verification_status(summary: dict[str, Any]) -> str:
    runs_obj = summary.get("runs")
    runs = runs_obj if isinstance(runs_obj, dict) else {}
    statuses: list[str] = []
    for item in runs.values():
        if not isinstance(item, dict):
            continue
        token = str(item.get("status") or "").strip().lower()
        if token:
            statuses.append(token)
    if not statuses:
        return str(summary.get("status") or "running").strip().lower() or "running"
    if any(token in {"queued", "pending", "running"} for token in statuses):
        return "running"
    if any(token in {"failed", "cancelled"} for token in statuses):
        return "failed"
    return "ok"


def allocate_verification_id(db: DB) -> str:
    for _ in range(8):
        candidate = f"ver-{secrets.token_hex(6)}"
        if db.fetch_one("SELECT id FROM verifications WHERE id=?", [candidate]) is None:
            return candidate
    return f"ver-{secrets.token_hex(8)}"


def verification_root(fs_manager: FsManager, verification_id: str) -> Path:
    return fs_manager.prepare_verification_root(str(verification_id or "").strip()).resolve()


def verification_run_root(fs_manager: FsManager, verification_id: str, run_id: str) -> Path:
    safe_run_id = str(run_id or "").strip() or "run"
    return fs_manager.prepare_verification_run_root(str(verification_id or "").strip(), safe_run_id).resolve()


def create_verification_record(
    db: DB,
    fs_manager: FsManager,
    *,
    verification_id: str,
    problem_id: int,
    workspace_id: int | None,
    source_commit: str = "",
    source_ref: str = "",
    kind: str,
    status: str,
    summary: dict[str, object] | None = None,
    artifact_path: str | Path | None = None,
) -> str:
    safe_id = str(verification_id or "").strip()
    if not safe_id:
        raise RuntimeError("verification id is required")
    with verification_update_lock(safe_id):
        existing = db.fetch_one("SELECT id,artifact_path FROM verifications WHERE id=?", [safe_id])
        if artifact_path is None:
            if existing is not None:
                existing_artifact_path = str(existing["artifact_path"] or "").strip()
                root = Path(existing_artifact_path).resolve() if existing_artifact_path else verification_root(fs_manager, safe_id)
            else:
                root = verification_root(fs_manager, safe_id)
        else:
            root = Path(str(artifact_path)).resolve()
        root.mkdir(parents=True, exist_ok=True)
        now_text = now_iso()
        encoded = json.dumps(_sanitize_verification_summary(summary or {}))
        params = [
            int(problem_id),
            int(workspace_id) if workspace_id is not None else None,
            str(source_commit or "").strip(),
            str(source_ref or "").strip(),
            str(kind or VERIFICATION_KIND_VERIFICATION).strip() or VERIFICATION_KIND_VERIFICATION,
            str(status or "").strip() or "running",
            encoded,
            str(root),
        ]
        if existing is None:
            db.execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [safe_id, *params, now_text, None],
            )
        else:
            db.execute(
                """
                UPDATE verifications
                SET problem_id=?,workspace_id=?,source_commit=?,source_ref=?,kind=?,status=?,summary_json=?,artifact_path=?
                WHERE id=?
                """,
                [*params, safe_id],
            )
        return str(root)


def load_verification_record(db: DB, verification_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        """
        SELECT id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
        FROM verifications
        WHERE id=?
        """,
        [str(verification_id or "").strip()],
    )
    return dict(row) if row is not None else None


def load_verification_summary(db: DB, verification_id: str) -> dict[str, object]:
    row = load_verification_record(db, verification_id)
    if row is None:
        return {}
    raw = str(row["summary_json"] or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _sanitize_verification_summary(parsed)


def list_verification_rows(
    db: DB,
    *,
    problem_id: int,
    workspace_id: int,
    limit: int,
    kinds: tuple[str, ...] = (VERIFICATION_KIND_VERIFICATION,),
) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    kind_tokens = [str(item or "").strip() for item in kinds if str(item or "").strip()]
    if not kind_tokens:
        kind_tokens = [VERIFICATION_KIND_VERIFICATION]
    placeholders = ",".join(("?" for _ in kind_tokens))
    rows = db.fetch_all(
        f"""
        SELECT id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
        FROM verifications
        WHERE problem_id=? AND workspace_id=? AND kind IN ({placeholders})
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(workspace_id), *kind_tokens, safe_limit],
    )
    return [dict(row) for row in rows]


def latest_verification_row(
    db: DB,
    *,
    problem_id: int,
    workspace_id: int,
    kind: str = VERIFICATION_KIND_VERIFICATION,
    ok_only: bool = False,
) -> dict[str, Any] | None:
    safe_kind = str(kind or VERIFICATION_KIND_VERIFICATION).strip() or VERIFICATION_KIND_VERIFICATION
    sql = """
        SELECT id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
        FROM verifications
        WHERE problem_id=? AND workspace_id=? AND kind=?
    """
    params: list[object] = [int(problem_id), int(workspace_id), safe_kind]
    if ok_only:
        sql += " AND status='ok'"
    sql += " ORDER BY created_at DESC LIMIT 1"
    row = db.fetch_one(sql, params)
    return dict(row) if row is not None else None


def latest_committed_verification_row(
    db: DB,
    *,
    problem_id: int,
    workspace_id: int,
    source_commit: str,
    kind: str = VERIFICATION_KIND_VERIFICATION,
    ok_only: bool = False,
) -> dict[str, Any] | None:
    safe_commit = str(source_commit or "").strip()
    if not safe_commit:
        return None
    safe_kind = str(kind or VERIFICATION_KIND_VERIFICATION).strip() or VERIFICATION_KIND_VERIFICATION
    sql = """
        SELECT id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
        FROM verifications
        WHERE problem_id=? AND workspace_id=? AND source_commit=? AND source_ref=? AND kind=?
    """
    params: list[object] = [int(problem_id), int(workspace_id), safe_commit, safe_commit, safe_kind]
    if ok_only:
        sql += " AND status='ok'"
    sql += " ORDER BY created_at DESC LIMIT 1"
    row = db.fetch_one(sql, params)
    return dict(row) if row is not None else None


def save_verification_summary(
    db: DB,
    *,
    verification_id: str,
    status: str,
    summary: dict[str, object],
    finished: bool = False,
) -> None:
    safe_id = str(verification_id or "").strip()
    if not safe_id:
        raise RuntimeError("verification id is required")
    with verification_update_lock(safe_id):
        finished_at = now_iso() if finished else None
        sanitized_summary = _sanitize_verification_summary(summary)
        if finished:
            db.execute(
                "UPDATE verifications SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [str(status or "").strip() or "failed", json.dumps(sanitized_summary), finished_at, safe_id],
            )
        else:
            db.execute(
                "UPDATE verifications SET status=?, summary_json=?, finished_at=NULL WHERE id=?",
                [str(status or "").strip() or "running", json.dumps(sanitized_summary), safe_id],
            )


def default_verification_summary(
    *,
    kind: str,
    mode: str,
    source_commit: str = "",
    source_ref: str = "",
    source_paths: list[str] | None = None,
    verification_source: str = "",
) -> dict[str, Any]:
    return {
        "kind": str(kind or VERIFICATION_KIND_VERIFICATION).strip() or VERIFICATION_KIND_VERIFICATION,
        "mode": str(mode or "").strip() or "pass-fail",
        "source_commit": str(source_commit or "").strip(),
        "source_ref": str(source_ref or "").strip(),
        "status": "running",
        "source_paths": list(source_paths or []),
        "verification_source": str(verification_source or "").strip() or "run.execute",
        "error": "",
        "updated_at": "",
        "finished_at": "",
        "artifact_root": "",
        "runs_order": [],
        "runs": {},
        "tests": [],
        "lifecycle": {"steps": []},
    }


def default_verification_run(
    *,
    run_id: str,
    source_label: str,
    expected_behavior: str,
    run_status: str = "running",
    artifact_path: str = "",
    task_kind: str = "",
) -> dict[str, Any]:
    safe_run_id = str(run_id or "").strip()
    if not safe_run_id:
        raise RuntimeError("run id is required")
    return {
        "run_id": safe_run_id,
        "status": str(run_status or "").strip().lower() or "running",
        "source_label": str(source_label or "").strip() or safe_run_id,
        "expected_behavior": str(expected_behavior or "unknown").strip() or "unknown",
        "artifact_path": str(artifact_path or "").strip(),
        "task_kind": str(task_kind or "").strip(),
        "summary": {},
    }


def verification_runs(summary: dict[str, Any]) -> dict[str, Any]:
    runs_obj = summary.get("runs")
    return runs_obj if isinstance(runs_obj, dict) else {}


def verification_run_ids(summary: dict[str, Any]) -> list[str]:
    values: list[str] = []
    order_obj = summary.get("runs_order")
    if isinstance(order_obj, list):
        for raw in order_obj:
            token = str(raw or "").strip()
            if token and token not in values:
                values.append(token)
    runs_obj = summary.get("runs")
    if isinstance(runs_obj, dict):
        for raw in runs_obj.keys():
            token = str(raw or "").strip()
            if token and token not in values:
                values.append(token)
    return values


def _is_solution_source_path(value: object) -> bool:
    token = str(value or "").strip().replace("\\", "/").lstrip("./")
    return bool(token) and token.startswith("solutions/")


def verification_source_paths(summary: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("source_paths", "submission_paths"):
        raw_paths = summary.get(key)
        if isinstance(raw_paths, list):
            for raw in raw_paths:
                token = str(raw or "").strip()
                if token and token not in values:
                    values.append(token)
    runs_obj = summary.get("runs")
    runs = runs_obj if isinstance(runs_obj, dict) else {}
    for item in runs.values():
        if not isinstance(item, dict):
            continue
        token = str(item.get("source_label") or "").strip()
        if _is_solution_source_path(token) and token not in values:
            values.append(token)
            continue
        run_summary = item.get("summary")
        if isinstance(run_summary, dict):
            token = str(run_summary.get("source") or "").strip()
            if _is_solution_source_path(token) and token not in values:
                values.append(token)
    return values


def verification_stage_results(summary: dict[str, Any]) -> dict[str, Any]:
    stage_results_obj = summary.get("stage_results")
    stage_results = stage_results_obj if isinstance(stage_results_obj, dict) else {}
    sanitized: dict[str, Any] = {}
    for key, item in stage_results.items():
        if not isinstance(item, dict):
            continue
        token = str(key or "").strip()
        if not token:
            continue
        sanitized[token] = _sanitize_verification_run_summary(dict(item))
    return sanitized


def verification_stage_summary(summary: dict[str, Any], stage_key: str) -> dict[str, Any]:
    token = str(stage_key or "").strip()
    if not token:
        return {}
    row = verification_stage_results(summary).get(token)
    return dict(row) if isinstance(row, dict) else {}


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _sanitize_verification_run_summary(run_summary: dict[str, Any]) -> dict[str, Any]:
    payload = _dict_or_empty(run_summary)
    for legacy_key in (
        "run_id",
        "run_ids",
        "run_count",
        "artifact_verification_status",
        "artifact_verification_error",
        "artifact_failed_step",
        "artifact_failed_test",
        "build_id",
        "build_ref",
        "verification_kind",
        "invocation",
        "invocation_id",
        "invocation_source",
    ):
        payload.pop(legacy_key, None)
    return payload


def _sanitize_verification_summary(summary: dict[str, Any]) -> dict[str, Any]:
    payload = _dict_or_empty(summary)
    for legacy_key in (
        "run_id",
        "run_ids",
        "run_count",
        "build_id",
        "build_ref",
        "verification_kind",
        "invocation",
        "invocation_id",
        "invocation_source",
    ):
        payload.pop(legacy_key, None)
    runs_obj = payload.get("runs")
    runs = runs_obj if isinstance(runs_obj, dict) else {}
    sanitized_runs: dict[str, Any] = {}
    for key, item in runs.items():
        if not isinstance(item, dict):
            continue
        run_row = dict(item)
        run_row["run_id"] = str(run_row.get("run_id") or key).strip() or str(key)
        run_row.pop("key", None)
        run_summary = run_row.get("summary")
        if isinstance(run_summary, dict):
            run_row["summary"] = _sanitize_verification_run_summary(run_summary)
        sanitized_runs[str(key)] = run_row
    payload["runs"] = sanitized_runs if sanitized_runs else {}
    order_obj = payload.get("runs_order")
    order = order_obj if isinstance(order_obj, list) else []
    sanitized_order: list[str] = []
    for raw in order:
        token = str(raw or "").strip()
        if token and token not in sanitized_order:
            sanitized_order.append(token)
    for token in sanitized_runs.keys():
        if token not in sanitized_order:
            sanitized_order.append(token)
    payload["runs_order"] = sanitized_order
    stage_results = verification_stage_results(payload)
    if stage_results:
        payload["stage_results"] = stage_results
    else:
        payload.pop("stage_results", None)
    return payload


def verification_run(summary: dict[str, Any], run_id: str) -> dict[str, Any]:
    safe_key = str(run_id or "").strip()
    if not safe_key:
        return {}
    row = verification_runs(summary).get(safe_key)
    return dict(row) if isinstance(row, dict) else {}


def load_verification_run(
    db: DB,
    *,
    verification_id: str,
    run_id: str,
) -> dict[str, Any]:
    summary = load_verification_summary(db, verification_id)
    return verification_run(summary, run_id)


def set_verification_run(
    summary: dict[str, Any],
    *,
    run_id: str,
    run: dict[str, Any],
) -> dict[str, Any]:
    safe_run_id = str(run_id or "").strip()
    if not safe_run_id:
        raise RuntimeError("run id is required")
    runs = verification_runs(summary)
    order_obj = summary.get("runs_order")
    order = order_obj if isinstance(order_obj, list) else []
    runs[safe_run_id] = _dict_or_empty(run)
    if safe_run_id not in order:
        order.append(safe_run_id)
    summary["runs"] = runs
    summary["runs_order"] = order
    summary["status"] = _normalize_verification_status(summary)
    return summary


def merge_verification_run_summary(
    summary: dict[str, Any],
    *,
    run_id: str,
    run_status: str,
    run_summary: dict[str, Any],
    source_label: str = "",
    expected_behavior: str = "",
    artifact_path: str = "",
    task_kind: str = "",
) -> dict[str, Any]:
    row = verification_run(summary, run_id)
    if not row:
        row = default_verification_run(
            run_id=run_id,
            source_label=source_label,
            expected_behavior=expected_behavior,
            run_status=run_status,
            artifact_path=artifact_path,
            task_kind=task_kind,
        )
    if source_label:
        row["source_label"] = str(source_label).strip()
    if expected_behavior:
        row["expected_behavior"] = str(expected_behavior).strip()
    if artifact_path:
        row["artifact_path"] = str(artifact_path).strip()
    if task_kind:
        row["task_kind"] = str(task_kind).strip()
    row["status"] = str(run_status or row.get("status") or "").strip().lower() or "running"
    row["summary"] = _sanitize_verification_run_summary(_dict_or_empty(run_summary))
    return set_verification_run(summary, run_id=run_id, run=row)


def save_verification_run_summary(
    db: DB,
    fs_manager: FsManager,
    *,
    verification_id: str,
    problem_id: int,
    workspace_id: int | None,
    source_commit: str = "",
    source_ref: str = "",
    kind: str,
    mode: str,
    verification_source: str,
    source_paths: list[str] | None,
    run_id: str,
    run_status: str,
    source_label: str,
    expected_behavior: str,
    run_summary: dict[str, Any],
    artifact_path: str = "",
    task_kind: str = "",
    error_text: str = "",
    finished: bool = False,
) -> dict[str, Any]:
    safe_id = str(verification_id or "").strip()
    if not safe_id:
        raise RuntimeError("verification id is required")
    with verification_update_lock(safe_id):
        record = load_verification_record(db, safe_id)
        if record is None:
            base_summary = default_verification_summary(
                kind=kind,
                mode=mode,
                source_commit=source_commit,
                source_ref=source_ref,
                source_paths=list(source_paths or []),
                verification_source=verification_source,
            )
            base_summary["artifact_root"] = str(verification_root(fs_manager, safe_id))
            create_verification_record(
                db,
                fs_manager,
                verification_id=safe_id,
                problem_id=int(problem_id),
                workspace_id=int(workspace_id) if workspace_id is not None else None,
                source_commit=str(source_commit or "").strip(),
                source_ref=str(source_ref or "").strip(),
                kind=str(kind or VERIFICATION_KIND_VERIFICATION).strip() or VERIFICATION_KIND_VERIFICATION,
                status="running",
                summary=base_summary,
            )
            summary_obj = base_summary
        else:
            summary_obj = load_verification_summary(db, safe_id)
        if source_paths:
            merged_source_paths = verification_source_paths(summary_obj)
            for raw in source_paths:
                token = str(raw or "").strip()
                if token and token not in merged_source_paths:
                    merged_source_paths.append(token)
            summary_obj["source_paths"] = merged_source_paths
        if verification_source:
            summary_obj["verification_source"] = str(verification_source).strip()
        if mode:
            summary_obj["mode"] = str(mode).strip()
        if source_commit:
            summary_obj["source_commit"] = str(source_commit).strip()
        if source_ref:
            summary_obj["source_ref"] = str(source_ref).strip()
        summary_obj["updated_at"] = now_iso()
        if error_text:
            summary_obj["error"] = str(error_text).strip()
        run_artifact_verification_id = str(run_summary.get("artifact_verification_id") or "").strip()
        if run_artifact_verification_id:
            summary_obj["artifact_verification_id"] = run_artifact_verification_id
        merge_verification_run_summary(
            summary_obj,
            run_id=run_id,
            run_status=run_status,
            run_summary=run_summary,
            source_label=source_label,
            expected_behavior=expected_behavior,
            artifact_path=artifact_path,
            task_kind=task_kind,
        )
        final_status = str(summary_obj.get("status") or "running").strip() or "running"
        final_finished = final_status not in {"queued", "pending", "running"}
        if final_finished:
            summary_obj["finished_at"] = now_iso()
        else:
            summary_obj["finished_at"] = ""
        summary_obj = _sanitize_verification_summary(summary_obj)
        save_verification_summary(
            db,
            verification_id=safe_id,
            status=final_status,
            summary=summary_obj,
            finished=final_finished,
        )
        return summary_obj


def upsert_verification_run(
    summary: dict[str, Any],
    *,
    run_id: str,
    run_status: str,
    source_label: str,
    expected_behavior: str,
    run_summary: dict[str, Any],
    artifact_path: str = "",
) -> dict[str, Any]:
    safe_run_id = str(run_id or "").strip()
    if not safe_run_id:
        raise RuntimeError("run id is required")
    runs_obj = summary.get("runs")
    runs = runs_obj if isinstance(runs_obj, dict) else {}
    order_obj = summary.get("runs_order")
    order = order_obj if isinstance(order_obj, list) else []
    row = {
        "run_id": safe_run_id,
        "status": str(run_status or "").strip().lower() or "running",
        "source_label": str(source_label or "").strip() or safe_run_id,
        "expected_behavior": str(expected_behavior or "unknown").strip() or "unknown",
        "artifact_path": str(artifact_path or "").strip(),
        "summary": _sanitize_verification_run_summary(_dict_or_empty(run_summary)),
    }
    runs[safe_run_id] = row
    if safe_run_id not in order:
        order.append(safe_run_id)
    summary["runs"] = runs
    summary["runs_order"] = order
    summary["status"] = _normalize_verification_status(summary)
    return summary
