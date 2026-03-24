from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import re
from typing import TypedDict, cast

from app.db import DB, now_iso
from app.service.disk.verification_store import VerificationStore


class VerificationTaskRow(TypedDict):
    id: str
    verification_id: str
    predecessor_task_id: str
    task_kind: str
    source_path: str
    logical_run_id: str
    test_name: str
    expected_behavior: str
    queue_index: int
    status: str
    verdict: str
    run_id: str
    judgehost_task_id: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    compile_log: str
    compile_log_truncated: int
    compile_log_total_chars: int
    diagnostics_json: str
    diagnostics_truncated: int
    diagnostics_total: int
    error_text: str
    error_text_truncated: int
    error_text_total_chars: int
    feedback_text: str
    feedback_text_truncated: int
    feedback_text_total_chars: int
    output_ref: str
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str


class VerificationTaskEdgeRow(TypedDict):
    parent_task_id: str
    child_task_id: str
    created_at: str


class VerificationTaskListRow(TypedDict):
    id: str
    verification_id: str
    task_kind: str
    source_path: str
    logical_run_id: str
    test_name: str
    expected_behavior: str
    status: str
    verdict: str


class VerificationTaskReadRow(TypedDict):
    id: str
    task_kind: str
    source_path: str
    logical_run_id: str
    test_name: str
    status: str


@dataclass
class _RuntimeTaskState:
    status: str
    run_id: str
    judgehost_task_id: str
    started_at: str


def _task_kind_rank(task_kind: str) -> int:
    if task_kind == "generate-input":
        return 0
    if task_kind == "main-correct":
        return 1
    if task_kind == "solution-run":
        return 2
    return 9


_TEST_NAME_NUM_RE = re.compile(r"^(\d+)\.in$")


def _test_name_order(test_name: str) -> tuple[int, str]:
    token = str(test_name or "")
    match = _TEST_NAME_NUM_RE.fullmatch(token)
    if match is not None:
        return (int(match.group(1)), token)
    return (10**9, token)


class VerificationTaskStore:
    TASK_PENDING = "pending"
    TASK_QUEUED = "queued"
    TASK_LEASED = "leased"
    TASK_DONE = "done"
    TASK_FAILED = "failed"
    TASK_CANCELLED = "cancelled"

    _shared_runtime_by_task_id: dict[str, _RuntimeTaskState] = {}
    _shared_logical_run_id_by_task_id: dict[str, str] = {}
    _shared_fail_reason_by_verification_id: dict[str, str] = {}

    def __init__(self, db: DB) -> None:
        self.db = db
        self._verification_store = VerificationStore(db)
        self._runtime_by_task_id = self._shared_runtime_by_task_id
        self._logical_run_id_by_task_id = self._shared_logical_run_id_by_task_id
        self._fail_reason_by_verification_id = self._shared_fail_reason_by_verification_id

    def allocate_id(self) -> str:
        for _ in range(8):
            candidate = f"vt-{secrets.token_hex(6)}"
            if self.db.fetch_one("SELECT id FROM verification_tasks WHERE id=?", [candidate]) is None:
                return candidate
        return f"vt-{secrets.token_hex(8)}"

    def _verification_root(self, verification_id: str) -> Path:
        return self._verification_store._verification_root(verification_id)

    def _result_bundle_root(self, verification_id: str, task_id: str) -> tuple[str, Path]:
        rel = f"tasks/{task_id}"
        root = (self._verification_root(verification_id) / rel).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return (rel, root)

    def _write_result_bundle(
        self,
        *,
        verification_id: str,
        task_id: str,
        compile_log: str,
        compile_log_truncated: bool,
        compile_log_total_chars: int,
        diagnostics_json: str,
        diagnostics_truncated: bool,
        diagnostics_total: int,
        error_text: str,
        error_text_truncated: bool,
        error_text_total_chars: int,
        feedback_text: str,
        feedback_text_truncated: bool,
        feedback_text_total_chars: int,
        output_ref: str,
        logical_run_id: str,
    ) -> str:
        bundle_ref, bundle_root = self._result_bundle_root(verification_id, task_id)
        payload = {
            "compile_log": compile_log,
            "compile_log_truncated": bool(compile_log_truncated),
            "compile_log_total_chars": int(compile_log_total_chars),
            "diagnostics_json": diagnostics_json,
            "diagnostics_truncated": bool(diagnostics_truncated),
            "diagnostics_total": int(diagnostics_total),
            "error_text": error_text,
            "error_text_truncated": bool(error_text_truncated),
            "error_text_total_chars": int(error_text_total_chars),
            "feedback_text": feedback_text,
            "feedback_text_truncated": bool(feedback_text_truncated),
            "feedback_text_total_chars": int(feedback_text_total_chars),
            "output_ref": output_ref,
            "logical_run_id": logical_run_id,
        }
        (bundle_root / "metadata.json").write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return bundle_ref

    def _load_result_bundle(self, verification_root: Path, result_bundle_ref: str) -> dict[str, object]:
        if not result_bundle_ref:
            return {}
        try:
            text = (verification_root / result_bundle_ref / "metadata.json").read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def replace_graph(
        self,
        verification_id: str,
        *,
        tasks: list[dict[str, object]],
        edges: list[tuple[str, str]],
    ) -> None:
        now_text = now_iso()
        predecessor_by_child = {child_id: parent_id for parent_id, child_id in edges}
        runtime_initial: dict[str, _RuntimeTaskState] = {}
        logical_run_ids: dict[str, str] = {}

        def _tx(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM verification_tasks WHERE verification_id=?", [verification_id])
            for item in tasks:
                task_id = str(item["id"])
                initial_status = str(item.get("status") or self.TASK_PENDING)
                logical_run_ids[task_id] = str(item.get("logical_run_id") or "")
                final_status = ""
                runtime_sec = item.get("runtime_sec")
                cpu_sec = item.get("cpu_sec")
                wall_sec = item.get("wall_sec")
                memory_kb = item.get("memory_kb")
                finished_at = None
                if initial_status in {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED}:
                    final_status = initial_status
                    finished_at = now_text
                elif initial_status in {self.TASK_QUEUED, self.TASK_LEASED}:
                    runtime_initial[task_id] = _RuntimeTaskState(
                        status=initial_status,
                        run_id=str(item.get("run_id") or ""),
                        judgehost_task_id=str(item.get("judgehost_task_id") or ""),
                        started_at=now_text if initial_status == self.TASK_LEASED else "",
                    )
                conn.execute(
                    """
                    INSERT INTO verification_tasks(
                        id,verification_id,predecessor_task_id,task_kind,source_path,test_name,expected_behavior,
                        final_status,verdict,runtime_sec,cpu_sec,wall_sec,memory_kb,result_bundle_ref,finished_at,created_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        task_id,
                        verification_id,
                        predecessor_by_child.get(task_id),
                        str(item.get("task_kind") or ""),
                        str(item.get("source_path") or ""),
                        str(item.get("test_name") or ""),
                        str(item.get("expected_behavior") or ""),
                        final_status,
                        str(item.get("verdict") or ""),
                        runtime_sec,
                        cpu_sec,
                        wall_sec,
                        memory_kb,
                        "",
                        finished_at,
                        now_text,
                    ],
                )
            conn.execute("UPDATE verifications SET fail_reason='' WHERE id=?", [verification_id])
        self.db.write_transaction(_tx)
        task_ids = {str(item["id"]) for item in tasks}
        to_drop = [task_id for task_id in self._runtime_by_task_id.keys() if task_id in task_ids]
        for task_id in to_drop:
            self._runtime_by_task_id.pop(task_id, None)
        logical_ids_to_drop = [task_id for task_id in self._logical_run_id_by_task_id.keys() if task_id in task_ids]
        for task_id in logical_ids_to_drop:
            self._logical_run_id_by_task_id.pop(task_id, None)
        self._runtime_by_task_id.update(runtime_initial)
        self._logical_run_id_by_task_id.update(logical_run_ids)
        self._fail_reason_by_verification_id.pop(verification_id, None)

    def _runtime_status(self, row: dict[str, object]) -> _RuntimeTaskState | None:
        return self._runtime_by_task_id.get(str(row["id"]))

    def _row_order(self, row: dict[str, object]) -> tuple[object, ...]:
        return (
            _task_kind_rank(str(row["task_kind"] or "")),
            _test_name_order(str(row["test_name"] or "")),
            str(row["source_path"] or ""),
            str(row["id"] or ""),
        )

    def _decorate_row(self, index: int, row: dict[str, object], bundle_payload: dict[str, object]) -> VerificationTaskRow:
        verification_id = str(row["verification_id"] or "")
        runtime = self._runtime_status(row)
        final_status = str(row["final_status"] or "")
        if final_status:
            status = final_status
        elif runtime is not None:
            status = runtime.status
        else:
            status = self.TASK_PENDING
        created_at = str(row["created_at"] or "")
        started_at = runtime.started_at if runtime is not None and runtime.started_at else None
        finished_at = str(row["finished_at"] or "") or None
        updated_at = finished_at or started_at or created_at
        source_path = str(row["source_path"] or "")
        expected_behavior = str(row["expected_behavior"] or "")
        task_kind = str(row["task_kind"] or "")
        logical_run_id = str(bundle_payload.get("logical_run_id") or self._logical_run_id_by_task_id.get(str(row["id"] or "")) or "")
        return {
            "id": str(row["id"] or ""),
            "verification_id": verification_id,
            "predecessor_task_id": str(row["predecessor_task_id"] or ""),
            "task_kind": task_kind,
            "source_path": source_path,
            "logical_run_id": logical_run_id,
            "test_name": str(row["test_name"] or ""),
            "expected_behavior": expected_behavior,
            "queue_index": index,
            "status": status,
            "verdict": str(row["verdict"] or ""),
            "run_id": runtime.run_id if runtime is not None else "",
            "judgehost_task_id": runtime.judgehost_task_id if runtime is not None else "",
            "runtime_sec": cast(float | None, row["runtime_sec"]),
            "cpu_sec": cast(float | None, row["cpu_sec"]),
            "wall_sec": cast(float | None, row["wall_sec"]),
            "memory_kb": cast(int | None, row["memory_kb"]),
            "compile_log": str(bundle_payload.get("compile_log") or ""),
            "compile_log_truncated": int(bool(bundle_payload.get("compile_log_truncated"))),
            "compile_log_total_chars": int(bundle_payload.get("compile_log_total_chars") or 0),
            "diagnostics_json": str(bundle_payload.get("diagnostics_json") or "[]"),
            "diagnostics_truncated": int(bool(bundle_payload.get("diagnostics_truncated"))),
            "diagnostics_total": int(bundle_payload.get("diagnostics_total") or 0),
            "error_text": str(bundle_payload.get("error_text") or ""),
            "error_text_truncated": int(bool(bundle_payload.get("error_text_truncated"))),
            "error_text_total_chars": int(bundle_payload.get("error_text_total_chars") or 0),
            "feedback_text": str(bundle_payload.get("feedback_text") or ""),
            "feedback_text_truncated": int(bool(bundle_payload.get("feedback_text_truncated"))),
            "feedback_text_total_chars": int(bundle_payload.get("feedback_text_total_chars") or 0),
            "output_ref": str(bundle_payload.get("output_ref") or ""),
            "started_at": started_at,
            "finished_at": finished_at,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def _decorate_list_row(self, row: dict[str, object]) -> VerificationTaskListRow:
        runtime = self._runtime_status(row)
        final_status = str(row["final_status"] or "")
        if final_status:
            status = final_status
        elif runtime is not None:
            status = runtime.status
        else:
            status = self.TASK_PENDING
        task_id = str(row["id"] or "")
        return {
            "id": task_id,
            "verification_id": str(row["verification_id"] or ""),
            "task_kind": str(row["task_kind"] or ""),
            "source_path": str(row["source_path"] or ""),
            "logical_run_id": str(self._logical_run_id_by_task_id.get(task_id) or ""),
            "test_name": str(row["test_name"] or ""),
            "expected_behavior": str(row["expected_behavior"] or ""),
            "status": status,
            "verdict": str(row["verdict"] or ""),
        }

    def list_rows(self, verification_id: str) -> list[VerificationTaskRow]:
        rows = [dict(row) for row in self.db.fetch_all("SELECT * FROM verification_tasks WHERE verification_id=?", [verification_id])]
        ordered = sorted(rows, key=self._row_order)
        verification_root = self._verification_root(verification_id)
        bundle_payload_by_ref: dict[str, dict[str, object]] = {}
        for row in ordered:
            result_bundle_ref = str(row["result_bundle_ref"] or "")
            if not result_bundle_ref:
                continue
            if result_bundle_ref in bundle_payload_by_ref:
                continue
            bundle_payload_by_ref[result_bundle_ref] = self._load_result_bundle(verification_root, result_bundle_ref)
        return [
            self._decorate_row(index + 1, row, bundle_payload_by_ref.get(str(row["result_bundle_ref"] or ""), {}))
            for index, row in enumerate(ordered)
        ]

    def list_rows_for_list(self, verification_id: str) -> list[VerificationTaskListRow]:
        rows = [
            dict(row)
            for row in self.db.fetch_all(
                """
                SELECT id,verification_id,task_kind,source_path,test_name,expected_behavior,final_status,verdict
                FROM verification_tasks
                WHERE verification_id=?
                """,
                [verification_id],
            )
        ]
        ordered = sorted(rows, key=self._row_order)
        return [self._decorate_list_row(row) for row in ordered]

    def find_runtime_row_by_judgehost_case(self, judgehost_task_id: str, test_name: str) -> VerificationTaskRow | None:
        safe_task_id = str(judgehost_task_id or "")
        safe_test_name = str(test_name or "")
        if (not safe_task_id) or (not safe_test_name):
            return None
        candidate_ids = [
            task_id
            for task_id, runtime in self._runtime_by_task_id.items()
            if runtime.judgehost_task_id == safe_task_id
        ]
        if not candidate_ids:
            return None
        rows = self.db.fetch_all(
            f"SELECT * FROM verification_tasks WHERE id IN ({','.join('?' for _ in candidate_ids)})",
            candidate_ids,
        )
        for row in sorted((dict(item) for item in rows), key=self._row_order):
            if str(row["test_name"] or "") != safe_test_name:
                continue
            verification_id = str(row["verification_id"] or "")
            bundle_payload = {}
            result_bundle_ref = str(row["result_bundle_ref"] or "")
            if result_bundle_ref:
                bundle_payload = self._load_result_bundle(self._verification_root(verification_id), result_bundle_ref)
            return self._decorate_row(1, row, bundle_payload)
        return None

    def find_runtime_rows_by_judgehost_task_id(self, judgehost_task_id: str) -> list[VerificationTaskRow]:
        safe_task_id = str(judgehost_task_id or "")
        if not safe_task_id:
            return []
        candidate_ids = [
            task_id
            for task_id, runtime in self._runtime_by_task_id.items()
            if runtime.judgehost_task_id == safe_task_id
        ]
        if not candidate_ids:
            return []
        rows = self.db.fetch_all(
            f"SELECT * FROM verification_tasks WHERE id IN ({','.join('?' for _ in candidate_ids)})",
            candidate_ids,
        )
        ordered = sorted((dict(item) for item in rows), key=self._row_order)
        verification_root_by_id: dict[str, Path] = {}
        bundle_payload_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for row in ordered:
            verification_id = str(row["verification_id"] or "")
            result_bundle_ref = str(row["result_bundle_ref"] or "")
            if not result_bundle_ref:
                continue
            key = (verification_id, result_bundle_ref)
            if key in bundle_payload_by_key:
                continue
            verification_root = verification_root_by_id.get(verification_id)
            if verification_root is None:
                verification_root = self._verification_root(verification_id)
                verification_root_by_id[verification_id] = verification_root
            bundle_payload_by_key[key] = self._load_result_bundle(verification_root, result_bundle_ref)
        return [
            self._decorate_row(
                index + 1,
                row,
                bundle_payload_by_key.get((str(row["verification_id"] or ""), str(row["result_bundle_ref"] or "")), {}),
            )
            for index, row in enumerate(ordered)
        ]

    def list_edge_rows(self, verification_id: str) -> list[VerificationTaskEdgeRow]:
        rows = self.db.fetch_all(
            "SELECT id, predecessor_task_id, created_at FROM verification_tasks WHERE verification_id=? AND predecessor_task_id IS NOT NULL ORDER BY id ASC",
            [verification_id],
        )
        return [
            {
                "parent_task_id": str(row["predecessor_task_id"] or ""),
                "child_task_id": str(row["id"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
        ]

    def verification_ids_with_unfinished_tasks(self) -> list[str]:
        rows = self.db.fetch_all(
            "SELECT DISTINCT verification_id FROM verification_tasks WHERE final_status='' ORDER BY verification_id ASC"
        )
        return [str(row["verification_id"] or "") for row in rows if str(row["verification_id"] or "")]

    def set_task_queued(self, task_id: str, *, run_id: str, judgehost_task_id: str) -> None:
        self._runtime_by_task_id[task_id] = _RuntimeTaskState(
            status=self.TASK_QUEUED,
            run_id=run_id,
            judgehost_task_id=judgehost_task_id,
            started_at="",
        )

    def set_task_leased(self, task_id: str) -> None:
        current = self._runtime_by_task_id.get(task_id)
        if current is None:
            return
        self._runtime_by_task_id[task_id] = _RuntimeTaskState(
            status=self.TASK_LEASED,
            run_id=current.run_id,
            judgehost_task_id=current.judgehost_task_id,
            started_at=current.started_at or now_iso(),
        )

    def requeue_task(self, task_id: str) -> None:
        current = self._runtime_by_task_id.get(task_id)
        if current is None:
            return
        self._runtime_by_task_id[task_id] = _RuntimeTaskState(
            status=self.TASK_QUEUED,
            run_id=current.run_id,
            judgehost_task_id=current.judgehost_task_id,
            started_at=current.started_at,
        )

    def save_task_result(
        self,
        task_id: str,
        *,
        status: str,
        verdict: str,
        run_id: str,
        judgehost_task_id: str,
        runtime_sec: float | None,
        cpu_sec: float | None,
        wall_sec: float | None,
        memory_kb: int | None,
        compile_log: str,
        compile_log_truncated: bool,
        compile_log_total_chars: int,
        diagnostics_json: str,
        diagnostics_truncated: bool,
        diagnostics_total: int,
        error_text: str,
        error_text_truncated: bool,
        error_text_total_chars: int,
        feedback_text: str,
        feedback_text_truncated: bool,
        feedback_text_total_chars: int,
        output_ref: str,
    ) -> None:
        row = self.db.fetch_one("SELECT verification_id FROM verification_tasks WHERE id=?", [task_id])
        if row is None:
            return
        verification_id = str(row["verification_id"] or "")
        logical_run_id = self._logical_run_id_by_task_id.get(task_id, "")
        bundle_ref = self._write_result_bundle(
            verification_id=verification_id,
            task_id=task_id,
            compile_log=compile_log,
            compile_log_truncated=compile_log_truncated,
            compile_log_total_chars=compile_log_total_chars,
            diagnostics_json=diagnostics_json,
            diagnostics_truncated=diagnostics_truncated,
            diagnostics_total=diagnostics_total,
            error_text=error_text,
            error_text_truncated=error_text_truncated,
            error_text_total_chars=error_text_total_chars,
            feedback_text=feedback_text,
            feedback_text_truncated=feedback_text_truncated,
            feedback_text_total_chars=feedback_text_total_chars,
            output_ref=output_ref,
            logical_run_id=logical_run_id,
        )
        finished_at = now_iso() if status in {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED} else None
        self.db.execute(
            """
            UPDATE verification_tasks
            SET final_status=?, verdict=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?, result_bundle_ref=?, finished_at=?
            WHERE id=? AND final_status=''
            """,
            [status, verdict, runtime_sec, cpu_sec, wall_sec, memory_kb, bundle_ref, finished_at, task_id],
        )
        self._runtime_by_task_id.pop(task_id, None)

    def cancel_unfinished_tasks(self, verification_id: str, *, reason: str) -> None:
        finished_at = now_iso()
        for row in self.list_rows(verification_id):
            if str(row["status"] or "") in {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED}:
                continue
            self.save_task_result(
                str(row["id"]),
                status=self.TASK_CANCELLED,
                verdict="",
                run_id=str(row["run_id"] or ""),
                judgehost_task_id=str(row["judgehost_task_id"] or ""),
                runtime_sec=None,
                cpu_sec=None,
                wall_sec=None,
                memory_kb=None,
                compile_log="",
                compile_log_truncated=False,
                compile_log_total_chars=0,
                diagnostics_json="[]",
                diagnostics_truncated=False,
                diagnostics_total=0,
                error_text=reason,
                error_text_truncated=False,
                error_text_total_chars=len(reason),
                feedback_text="",
                feedback_text_truncated=False,
                feedback_text_total_chars=0,
                output_ref="",
            )
        self.db.execute(
            "UPDATE verifications SET finished_at=COALESCE(finished_at, ?), fail_reason=CASE WHEN fail_reason='' THEN ? ELSE fail_reason END WHERE id=?",
            [finished_at, reason, verification_id],
        )

    def cancel_not_started_tasks(
        self,
        verification_id: str,
        *,
        reason: str,
        protected_run_ids: set[str] | None = None,
    ) -> None:
        safe_protected_run_ids = protected_run_ids or set()
        for row in self.list_rows(verification_id):
            if str(row["status"] or "") not in {self.TASK_PENDING, self.TASK_QUEUED}:
                continue
            run_id = str(row["run_id"] or "")
            if run_id in safe_protected_run_ids:
                continue
            self.save_task_result(
                str(row["id"]),
                status=self.TASK_CANCELLED,
                verdict="",
                run_id=str(row["run_id"] or ""),
                judgehost_task_id=str(row["judgehost_task_id"] or ""),
                runtime_sec=None,
                cpu_sec=None,
                wall_sec=None,
                memory_kb=None,
                compile_log="",
                compile_log_truncated=False,
                compile_log_total_chars=0,
                diagnostics_json="[]",
                diagnostics_truncated=False,
                diagnostics_total=0,
                error_text=reason,
                error_text_truncated=False,
                error_text_total_chars=len(reason),
                feedback_text="",
                feedback_text_truncated=False,
                feedback_text_total_chars=0,
                output_ref="",
            )

    def set_fail_flag(self, verification_id: str, *, reason: str) -> None:
        self._fail_reason_by_verification_id[verification_id] = reason

    def clear_fail_flag(self, verification_id: str) -> None:
        self._fail_reason_by_verification_id.pop(verification_id, None)

    def fail_state(self, verification_id: str) -> tuple[bool, str]:
        fail_reason = self._fail_reason_by_verification_id.get(verification_id, "")
        return (bool(fail_reason), fail_reason)
