from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
import re
from typing import TypedDict, cast

from app.db import DB, now_iso
from app.service.platform.error_text import aux_display_text_limit_bytes, bounded_display_text
from app.service.verification.task_metadata import normalize_diagnostics_json_text


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
    diagnostics_json: str
    error_text: str
    feedback_text: str
    output_ref: str
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str


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
        self._runtime_by_task_id = self._shared_runtime_by_task_id
        self._logical_run_id_by_task_id = self._shared_logical_run_id_by_task_id
        self._fail_reason_by_verification_id = self._shared_fail_reason_by_verification_id

    @staticmethod
    def _limit_bytes() -> int:
        return aux_display_text_limit_bytes()

    @classmethod
    def _normalize_display_text(cls, value: str) -> str:
        return bounded_display_text(value, limit_bytes=cls._limit_bytes())

    def allocate_id(self) -> str:
        for _ in range(8):
            candidate = f"vt-{secrets.token_hex(6)}"
            if self.db.fetch_one("SELECT id FROM verification_tasks WHERE id=?", [candidate]) is None:
                return candidate
        return f"vt-{secrets.token_hex(8)}"

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
                        id,verification_id,predecessor_task_id,task_kind,source_path,logical_run_id,test_name,expected_behavior,
                        final_status,verdict,runtime_sec,cpu_sec,wall_sec,memory_kb,compile_log,diagnostics_json,error_text,feedback_text,output_ref,finished_at,created_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        task_id,
                        verification_id,
                        predecessor_by_child.get(task_id),
                        str(item.get("task_kind") or ""),
                        str(item.get("source_path") or ""),
                        logical_run_ids[task_id],
                        str(item.get("test_name") or ""),
                        str(item.get("expected_behavior") or ""),
                        final_status,
                        str(item.get("verdict") or ""),
                        runtime_sec,
                        cpu_sec,
                        wall_sec,
                        memory_kb,
                        self._normalize_display_text(str(item.get("compile_log") or "")),
                        normalize_diagnostics_json_text(
                            str(item.get("diagnostics_json") or "[]"),
                            message_limit=self._limit_bytes(),
                        ),
                        self._normalize_display_text(str(item.get("error_text") or "")),
                        self._normalize_display_text(str(item.get("feedback_text") or "")),
                        str(item.get("output_ref") or ""),
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

    def _decorate_row(self, index: int, row: dict[str, object]) -> VerificationTaskRow:
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
        task_id = str(row["id"] or "")
        logical_run_id = str(row["logical_run_id"] or self._logical_run_id_by_task_id.get(task_id) or "")
        return {
            "id": task_id,
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
            "compile_log": str(row["compile_log"] or ""),
            "diagnostics_json": str(row["diagnostics_json"] or "[]"),
            "error_text": str(row["error_text"] or ""),
            "feedback_text": str(row["feedback_text"] or ""),
            "output_ref": str(row["output_ref"] or ""),
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
        return [self._decorate_row(index + 1, row) for index, row in enumerate(ordered)]

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
            return self._decorate_row(1, row)
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
        return [self._decorate_row(index + 1, row) for index, row in enumerate(ordered)]

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
        diagnostics_json: str,
        error_text: str,
        feedback_text: str,
        output_ref: str,
    ) -> None:
        logical_run_id = self._logical_run_id_by_task_id.get(task_id, "")
        finished_at = now_iso() if status in {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED} else None
        limit_bytes = self._limit_bytes()
        safe_compile_log = bounded_display_text(compile_log, limit_bytes=limit_bytes)
        safe_error_text = bounded_display_text(error_text, limit_bytes=limit_bytes)
        safe_feedback_text = bounded_display_text(feedback_text, limit_bytes=limit_bytes)
        safe_diagnostics_json = normalize_diagnostics_json_text(
            diagnostics_json,
            message_limit=limit_bytes,
        )
        self.db.execute(
            """
            UPDATE verification_tasks
            SET final_status=?, verdict=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?, logical_run_id=?, compile_log=?, diagnostics_json=?, error_text=?, feedback_text=?, output_ref=?, finished_at=?
            WHERE id=? AND final_status=''
            """,
            [
                status,
                verdict,
                runtime_sec,
                cpu_sec,
                wall_sec,
                memory_kb,
                logical_run_id,
                safe_compile_log,
                safe_diagnostics_json,
                safe_error_text,
                safe_feedback_text,
                output_ref,
                finished_at,
                task_id,
            ],
        )
        self._runtime_by_task_id.pop(task_id, None)

    def overwrite_task_result(
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
        diagnostics_json: str,
        error_text: str,
        feedback_text: str,
        output_ref: str,
    ) -> None:
        logical_run_id = self._logical_run_id_by_task_id.get(task_id, run_id)
        finished_at = now_iso() if status in {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED} else None
        limit_bytes = self._limit_bytes()
        safe_compile_log = bounded_display_text(compile_log, limit_bytes=limit_bytes)
        safe_error_text = bounded_display_text(error_text, limit_bytes=limit_bytes)
        safe_feedback_text = bounded_display_text(feedback_text, limit_bytes=limit_bytes)
        safe_diagnostics_json = normalize_diagnostics_json_text(
            diagnostics_json,
            message_limit=limit_bytes,
        )
        self.db.execute(
            """
            UPDATE verification_tasks
            SET final_status=?, verdict=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?, logical_run_id=?, compile_log=?, diagnostics_json=?, error_text=?, feedback_text=?, output_ref=?, finished_at=COALESCE(?, finished_at)
            WHERE id=?
            """,
            [
                status,
                verdict,
                runtime_sec,
                cpu_sec,
                wall_sec,
                memory_kb,
                logical_run_id,
                safe_compile_log,
                safe_diagnostics_json,
                safe_error_text,
                safe_feedback_text,
                output_ref,
                finished_at,
                task_id,
            ],
        )
        self._runtime_by_task_id.pop(task_id, None)

    def overwrite_fail_reason(self, verification_id: str, *, reason: str) -> None:
        safe_reason = self._normalize_display_text(reason)
        self.db.execute(
            "UPDATE verifications SET fail_reason=? WHERE id=?",
            [safe_reason, verification_id],
        )
        if safe_reason:
            self._fail_reason_by_verification_id[verification_id] = safe_reason
        else:
            self._fail_reason_by_verification_id.pop(verification_id, None)

    def cancel_unfinished_tasks(self, verification_id: str, *, reason: str) -> None:
        finished_at = now_iso()
        safe_reason = self._normalize_display_text(reason)
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
                diagnostics_json="[]",
                error_text=safe_reason,
                feedback_text="",
                output_ref="",
            )
        self.db.execute(
            "UPDATE verifications SET finished_at=COALESCE(finished_at, ?), fail_reason=CASE WHEN fail_reason='' THEN ? ELSE fail_reason END WHERE id=?",
            [finished_at, safe_reason, verification_id],
        )

    def cancel_not_started_tasks(
        self,
        verification_id: str,
        *,
        reason: str,
        protected_run_ids: set[str] | None = None,
    ) -> None:
        safe_reason = self._normalize_display_text(reason)
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
                diagnostics_json="[]",
                error_text=safe_reason,
                feedback_text="",
                output_ref="",
            )

    def set_fail_flag(self, verification_id: str, *, reason: str) -> None:
        if verification_id in self._fail_reason_by_verification_id:
            return
        self._fail_reason_by_verification_id[verification_id] = self._normalize_display_text(reason)

    def fail_state(self, verification_id: str) -> tuple[bool, str]:
        fail_reason = self._fail_reason_by_verification_id.get(verification_id, "")
        return (bool(fail_reason), fail_reason)
