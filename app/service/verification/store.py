from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any, cast

from app.db import DB, now_iso
from app.service.platform.fs.layout import FsManager

from .types import Kind, Status

VerificationSummary = dict[str, Any]
VerificationRunRow = dict[str, Any]

_VERIFICATION_UPDATE_LOCKS_GUARD = threading.Lock()
_VERIFICATION_UPDATE_LOCKS: dict[str, threading.RLock] = {}


def _summary_runs(summary: VerificationSummary) -> dict[str, VerificationRunRow]:
    runs = cast(dict[str, VerificationRunRow] | None, summary.get("runs"))
    return {} if runs is None else runs


def _summary_run_order(summary: VerificationSummary) -> list[str]:
    order = cast(list[str] | None, summary.get("runs_order"))
    return [] if order is None else order


def _summary_stage_results(summary: VerificationSummary) -> dict[str, VerificationSummary]:
    stage_results = cast(dict[str, VerificationSummary] | None, summary.get("stage_results"))
    return {} if stage_results is None else stage_results


def _run_summary(run_row: VerificationRunRow) -> VerificationSummary:
    summary = cast(VerificationSummary | None, run_row.get("summary"))
    return {} if summary is None else summary


def _canonical_verification_step_id(raw: object) -> str:
    if isinstance(raw, str):
        token = raw
    elif isinstance(raw, dict):
        step_obj = raw.get("step") or raw.get("id") or raw.get("step_id") or ""
        if not isinstance(step_obj, str):
            return ""
        token = step_obj
    else:
        return ""
    step_id = token.strip().lower().replace("-", "_")
    if step_id in {"compile", "generate", "gen", "generate_input"}:
        return "gen"
    if step_id in {"validate", "val", "generate_output", "generate_outputs"}:
        return "val"
    if step_id in {"solve", "run"}:
        return "run"
    if step_id in {"check", "check_expectations"}:
        return "check"
    return ""


def verification_update_lock(verification_id: str) -> threading.RLock:
    if not verification_id:
        raise RuntimeError("verification id is required")
    with _VERIFICATION_UPDATE_LOCKS_GUARD:
        lock = _VERIFICATION_UPDATE_LOCKS.get(verification_id)
        if lock is None:
            lock = threading.RLock()
            _VERIFICATION_UPDATE_LOCKS[verification_id] = lock
        return lock


def _normalize_verification_status(summary: VerificationSummary) -> str:
    statuses = [run_row["status"] for run_row in _summary_runs(summary).values()]
    if not statuses:
        return cast(str, summary["status"])
    if any(token in {Status.QUEUED.value, Status.PENDING.value, Status.RUNNING.value} for token in statuses):
        return Status.RUNNING.value
    if any(token in {Status.FAILED.value, Status.CANCELLED.value} for token in statuses):
        return Status.FAILED.value
    return Status.OK.value


def allocate_verification_id(db: DB) -> str:
    for _ in range(8):
        candidate = f"ver-{secrets.token_hex(6)}"
        if db.fetch_one("SELECT id FROM verifications WHERE id=?", [candidate]) is None:
            return candidate
    return f"ver-{secrets.token_hex(8)}"


def verification_root(fs_manager: FsManager, verification_id: str) -> Path:
    return fs_manager.prepare_verification_root(verification_id).resolve()


def verification_run_root(fs_manager: FsManager, verification_id: str, run_id: str) -> Path:
    safe_run_id = run_id or "run"
    return fs_manager.prepare_verification_run_root(verification_id, safe_run_id).resolve()


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
    if not verification_id:
        raise RuntimeError("verification id is required")
    with verification_update_lock(verification_id):
        existing = db.fetch_one("SELECT id,artifact_path FROM verifications WHERE id=?", [verification_id])
        if artifact_path is None:
            if existing is None:
                root = verification_root(fs_manager, verification_id)
            else:
                existing_artifact_path = cast(str | None, existing["artifact_path"])
                root = verification_root(fs_manager, verification_id) if existing_artifact_path is None or existing_artifact_path == "" else Path(existing_artifact_path).resolve()
        else:
            root = Path(artifact_path).resolve()
        root.mkdir(parents=True, exist_ok=True)
        now_text = now_iso()
        encoded = json.dumps(_sanitize_verification_summary(summary or {}))
        params = [
            int(problem_id),
            int(workspace_id) if workspace_id is not None else None,
            source_commit,
            source_ref,
            kind or Kind.VERIFICATION.value,
            status or Status.RUNNING.value,
            encoded,
            str(root),
        ]
        if existing is None:
            db.execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [verification_id, *params, now_text, None],
            )
        else:
            db.execute(
                """
                UPDATE verifications
                SET problem_id=?,workspace_id=?,source_commit=?,source_ref=?,kind=?,status=?,summary_json=?,artifact_path=?
                WHERE id=?
                """,
                [*params, verification_id],
            )
        return str(root)


def load_verification_record(db: DB, verification_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        """
        SELECT id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
        FROM verifications
        WHERE id=?
        """,
        [verification_id],
    )
    return None if row is None else dict(row)


def load_verification_summary(db: DB, verification_id: str) -> VerificationSummary:
    row = load_verification_record(db, verification_id)
    if row is None:
        return {}
    raw = cast(str | None, row["summary_json"]) or ""
    if not raw:
        return {}
    parsed = cast(VerificationSummary, json.loads(raw))
    return _sanitize_verification_summary(parsed)


def list_verification_rows(
    db: DB,
    *,
    problem_id: int,
    workspace_id: int,
    limit: int,
    kinds: tuple[str, ...] = (Kind.VERIFICATION.value,),
) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    kind_tokens = list(kinds) or [Kind.VERIFICATION.value]
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


def save_verification_summary(
    db: DB,
    *,
    verification_id: str,
    status: str,
    summary: VerificationSummary,
    finished: bool = False,
) -> None:
    if not verification_id:
        raise RuntimeError("verification id is required")
    with verification_update_lock(verification_id):
        finished_at = now_iso() if finished else None
        sanitized_summary = _sanitize_verification_summary(summary)
        if finished:
            db.execute(
                "UPDATE verifications SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [status or Status.FAILED.value, json.dumps(sanitized_summary), finished_at, verification_id],
            )
        else:
            db.execute(
                "UPDATE verifications SET status=?, summary_json=?, finished_at=NULL WHERE id=?",
                [status or Status.RUNNING.value, json.dumps(sanitized_summary), verification_id],
            )


def default_verification_summary(
    *,
    kind: str,
    mode: str,
    source_commit: str = "",
    source_ref: str = "",
    source_paths: list[str] | None = None,
    verification_source: str = "",
) -> VerificationSummary:
    return {
        "kind": kind or Kind.VERIFICATION.value,
        "mode": mode or "pass-fail",
        "source_commit": source_commit,
        "source_ref": source_ref,
        "status": Status.RUNNING.value,
        "source_paths": list(source_paths or []),
        "verification_source": verification_source or "run.execute",
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
    run_status: str = Status.RUNNING.value,
    artifact_path: str = "",
    task_kind: str = "",
) -> VerificationRunRow:
    if not run_id:
        raise RuntimeError("run id is required")
    return {
        "run_id": run_id,
        "status": run_status or Status.RUNNING.value,
        "source_label": source_label or run_id,
        "expected_behavior": expected_behavior or "unknown",
        "artifact_path": artifact_path,
        "task_kind": task_kind,
        "summary": {},
    }


def verification_run_ids(summary: VerificationSummary) -> list[str]:
    values: list[str] = []
    for token in _summary_run_order(summary):
        if token and token not in values:
            values.append(token)
    for token in _summary_runs(summary).keys():
        if token and token not in values:
            values.append(token)
    return values


def _is_solution_source_path(value: str) -> bool:
    token = value.replace("\\", "/").lstrip("./")
    return bool(token) and token.startswith("solutions/")


def verification_source_paths(summary: VerificationSummary) -> list[str]:
    values: list[str] = []
    for key in ("source_paths", "submission_paths"):
        raw_paths = cast(list[str] | None, summary.get(key))
        if raw_paths is None:
            continue
        for token in raw_paths:
            if token and _is_solution_source_path(token) and token not in values:
                values.append(token)
    for item in _summary_runs(summary).values():
        source_label = cast(str | None, item.get("source_label"))
        if source_label is not None and _is_solution_source_path(source_label) and source_label not in values:
            values.append(source_label)
            continue
        source = cast(str | None, _run_summary(item).get("source"))
        if source is not None and _is_solution_source_path(source) and source not in values:
            values.append(source)
    return values


def verification_stage_results(summary: VerificationSummary) -> dict[str, VerificationSummary]:
    sanitized: dict[str, VerificationSummary] = {}
    for key, item in _summary_stage_results(summary).items():
        if not key:
            continue
        sanitized[key] = _sanitize_verification_run_summary(item)
    return sanitized


def verification_stage_summary(summary: VerificationSummary, stage_key: str) -> VerificationSummary:
    return dict(verification_stage_results(summary).get(stage_key) or {})


def _sanitize_verification_run_summary(run_summary: VerificationSummary) -> VerificationSummary:
    payload = dict(run_summary)
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


def _sanitize_verification_summary(summary: VerificationSummary) -> VerificationSummary:
    payload = dict(summary)
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
    sanitized_runs: dict[str, VerificationRunRow] = {}
    for key, item in _summary_runs(payload).items():
        run_row = dict(item)
        run_row["run_id"] = key
        run_row.pop("key", None)
        run_summary = cast(VerificationSummary | None, run_row.get("summary"))
        if run_summary is not None:
            run_row["summary"] = _sanitize_verification_run_summary(run_summary)
        sanitized_runs[key] = run_row
    payload["runs"] = sanitized_runs
    sanitized_steps: list[str] = []
    raw_steps = payload.get("steps")
    if isinstance(raw_steps, list):
        for raw_step in raw_steps:
            step_id = _canonical_verification_step_id(raw_step)
            if step_id and step_id not in sanitized_steps:
                sanitized_steps.append(step_id)
    if sanitized_steps:
        payload["steps"] = sanitized_steps
    else:
        payload.pop("steps", None)
    sanitized_order = _summary_run_order(payload)
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


def verification_run(summary: VerificationSummary, run_id: str) -> VerificationRunRow:
    if not run_id:
        return {}
    return dict(_summary_runs(summary).get(run_id) or {})


def load_verification_run(
    db: DB,
    *,
    verification_id: str,
    run_id: str,
) -> VerificationRunRow:
    summary = load_verification_summary(db, verification_id)
    return verification_run(summary, run_id)


def set_verification_run(
    summary: VerificationSummary,
    *,
    run_id: str,
    run: VerificationRunRow,
) -> VerificationSummary:
    if not run_id:
        raise RuntimeError("run id is required")
    runs = _summary_runs(summary)
    order = _summary_run_order(summary)
    runs[run_id] = dict(run)
    if run_id not in order:
        order.append(run_id)
    summary["runs"] = runs
    summary["runs_order"] = order
    summary["status"] = _normalize_verification_status(summary)
    return summary


def merge_verification_run_summary(
    summary: VerificationSummary,
    *,
    run_id: str,
    run_status: str,
    run_summary: VerificationSummary,
    source_label: str = "",
    expected_behavior: str = "",
    artifact_path: str = "",
    task_kind: str = "",
) -> VerificationSummary:
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
        row["source_label"] = source_label
    if expected_behavior:
        row["expected_behavior"] = expected_behavior
    if artifact_path:
        row["artifact_path"] = artifact_path
    if task_kind:
        row["task_kind"] = task_kind
    row["status"] = run_status or cast(str, row["status"])
    row["summary"] = _sanitize_verification_run_summary(run_summary)
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
    run_summary: VerificationSummary,
    artifact_path: str = "",
    task_kind: str = "",
    error_text: str = "",
    finished: bool = False,
) -> VerificationSummary:
    del finished
    if not verification_id:
        raise RuntimeError("verification id is required")
    with verification_update_lock(verification_id):
        record = load_verification_record(db, verification_id)
        if record is None:
            summary_obj = default_verification_summary(
                kind=kind,
                mode=mode,
                source_commit=source_commit,
                source_ref=source_ref,
                source_paths=list(source_paths or []),
                verification_source=verification_source,
            )
            summary_obj["artifact_root"] = str(verification_root(fs_manager, verification_id))
            create_verification_record(
                db,
                fs_manager,
                verification_id=verification_id,
                problem_id=int(problem_id),
                workspace_id=int(workspace_id) if workspace_id is not None else None,
                source_commit=source_commit,
                source_ref=source_ref,
                kind=kind or Kind.VERIFICATION.value,
                status=Status.RUNNING.value,
                summary=summary_obj,
            )
        else:
            summary_obj = load_verification_summary(db, verification_id)
        if source_paths is not None:
            merged_source_paths = verification_source_paths(summary_obj)
            for token in source_paths:
                if token and token not in merged_source_paths:
                    merged_source_paths.append(token)
            summary_obj["source_paths"] = merged_source_paths
        if verification_source:
            summary_obj["verification_source"] = verification_source
        if mode:
            summary_obj["mode"] = mode
        if source_commit:
            summary_obj["source_commit"] = source_commit
        if source_ref:
            summary_obj["source_ref"] = source_ref
        summary_obj["updated_at"] = now_iso()
        if error_text:
            summary_obj["error"] = error_text
        artifact_verification_id = cast(str | None, run_summary.get("artifact_verification_id"))
        if artifact_verification_id is not None and artifact_verification_id != "":
            summary_obj["artifact_verification_id"] = artifact_verification_id
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
        final_status = cast(str, summary_obj["status"])
        final_finished = final_status not in {Status.QUEUED.value, Status.PENDING.value, Status.RUNNING.value}
        summary_obj["finished_at"] = now_iso() if final_finished else ""
        summary_obj = _sanitize_verification_summary(summary_obj)
        save_verification_summary(
            db,
            verification_id=verification_id,
            status=final_status,
            summary=summary_obj,
            finished=final_finished,
        )
        return summary_obj
