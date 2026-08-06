from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
import re
from typing import TypedDict, cast

from app.db import DB, now_iso
from app.service.platform.error_text import aux_display_text_limit_bytes, bounded_display_text
from app.service.platform.rwlock import WriterPriorityRWLock
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
    answer_correct: bool
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

    def __init__(self, db: DB) -> None:
        self.db = db
        self._runtime_lock = WriterPriorityRWLock()
        self._runtime_by_task_id: dict[str, _RuntimeTaskState] = {}
        self._logical_run_id_by_task_id: dict[str, str] = {}
        self._test_name_by_task_id: dict[str, str] = {}
        self._task_id_by_judgehost_case: dict[tuple[str, str], str] = {}
        self._task_ids_by_judgehost_task: dict[str, set[str]] = {}
        self._fail_reason_by_verification_id: dict[str, str] = {}

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
        test_names = {
            str(item["id"]): str(item.get("test_name") or "")
            for item in tasks
        }
        previous_task_ids = {
            str(row["id"])
            for row in self.db.fetch_all(
                "SELECT id FROM verification_tasks WHERE verification_id=?",
                [verification_id],
            )
        }

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
                        final_status,verdict,runtime_sec,cpu_sec,wall_sec,memory_kb,answer_correct,compile_log,diagnostics_json,error_text,feedback_text,output_ref,finished_at,created_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        1 if bool(item.get("answer_correct")) else 0,
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
        with self._runtime_lock.write_lock():
            for task_id in previous_task_ids | task_ids:
                self._remove_judgehost_indexes_locked(task_id)
                self._runtime_by_task_id.pop(task_id, None)
                self._logical_run_id_by_task_id.pop(task_id, None)
                self._test_name_by_task_id.pop(task_id, None)
            self._runtime_by_task_id.update(runtime_initial)
            self._logical_run_id_by_task_id.update(logical_run_ids)
            self._test_name_by_task_id.update(test_names)
            for task_id, runtime in runtime_initial.items():
                self._add_judgehost_indexes_locked(task_id, runtime)
            self._fail_reason_by_verification_id.pop(verification_id, None)

    def _remove_judgehost_indexes_locked(self, task_id: str) -> None:
        runtime = self._runtime_by_task_id.get(task_id)
        if runtime is None or not runtime.judgehost_task_id:
            return
        test_name = self._test_name_by_task_id.get(task_id, "")
        if test_name:
            self._task_id_by_judgehost_case.pop((runtime.judgehost_task_id, test_name), None)
        task_ids = self._task_ids_by_judgehost_task.get(runtime.judgehost_task_id)
        if task_ids is None:
            return
        task_ids.discard(task_id)
        if not task_ids:
            self._task_ids_by_judgehost_task.pop(runtime.judgehost_task_id, None)

    def _add_judgehost_indexes_locked(self, task_id: str, runtime: _RuntimeTaskState) -> None:
        if not runtime.judgehost_task_id:
            return
        test_name = self._test_name_by_task_id.get(task_id, "")
        if test_name:
            self._task_id_by_judgehost_case[(runtime.judgehost_task_id, test_name)] = task_id
        self._task_ids_by_judgehost_task.setdefault(runtime.judgehost_task_id, set()).add(task_id)

    def _runtime_status(self, row: dict[str, object]) -> _RuntimeTaskState | None:
        with self._runtime_lock.read_lock():
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
        with self._runtime_lock.read_lock():
            runtime_logical_run_id = self._logical_run_id_by_task_id.get(task_id, "")
        logical_run_id = str(row["logical_run_id"] or runtime_logical_run_id)
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
            "answer_correct": bool(row["answer_correct"]),
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
        with self._runtime_lock.read_lock():
            runtime_logical_run_id = self._logical_run_id_by_task_id.get(task_id, "")
        return {
            "id": task_id,
            "verification_id": str(row["verification_id"] or ""),
            "task_kind": str(row["task_kind"] or ""),
            "source_path": str(row["source_path"] or ""),
            "logical_run_id": str(row["logical_run_id"] or runtime_logical_run_id),
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
                SELECT id,verification_id,task_kind,source_path,logical_run_id,
                       test_name,expected_behavior,final_status,verdict
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
        with self._runtime_lock.read_lock():
            candidate_id = self._task_id_by_judgehost_case.get((safe_task_id, safe_test_name))
        if candidate_id is None:
            return None
        row = self.db.fetch_one("SELECT * FROM verification_tasks WHERE id=?", [candidate_id])
        return None if row is None else self._decorate_row(1, dict(row))

    def find_runtime_rows_by_judgehost_task_id(self, judgehost_task_id: str) -> list[VerificationTaskRow]:
        safe_task_id = str(judgehost_task_id or "")
        if not safe_task_id:
            return []
        with self._runtime_lock.read_lock():
            candidate_ids = list(self._task_ids_by_judgehost_task.get(safe_task_id, ()))
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
        with self._runtime_lock.write_lock():
            self._remove_judgehost_indexes_locked(task_id)
            runtime = _RuntimeTaskState(
                status=self.TASK_QUEUED,
                run_id=run_id,
                judgehost_task_id=judgehost_task_id,
                started_at="",
            )
            self._runtime_by_task_id[task_id] = runtime
            self._add_judgehost_indexes_locked(task_id, runtime)

    def set_task_leased(self, task_id: str) -> None:
        with self._runtime_lock.write_lock():
            current = self._runtime_by_task_id.get(task_id)
            if current is None:
                return
            self._runtime_by_task_id[task_id] = _RuntimeTaskState(
                status=self.TASK_LEASED,
                run_id=current.run_id,
                judgehost_task_id=current.judgehost_task_id,
                started_at=current.started_at or now_iso(),
            )

    def requeue_leased_tasks(
        self,
        verification_id: str,
        judgehost_task_ids: list[str],
    ) -> list[str]:
        allowed = {str(task_id) for task_id in judgehost_task_ids if str(task_id)}
        if not allowed:
            return []
        with self._runtime_lock.read_lock():
            candidates = [
                (task_id, runtime)
                for task_id, runtime in self._runtime_by_task_id.items()
                if runtime.status == self.TASK_LEASED
                and runtime.judgehost_task_id in allowed
            ]
        eligible: list[tuple[str, _RuntimeTaskState]] = []
        for task_id, runtime in candidates:
            row = self.db.fetch_one(
                "SELECT verification_id, final_status FROM verification_tasks WHERE id=?",
                [task_id],
            )
            if row is None or str(row["verification_id"] or "") != verification_id:
                continue
            if str(row["final_status"] or ""):
                continue
            eligible.append((task_id, runtime))
        changed: list[str] = []
        with self._runtime_lock.write_lock():
            for task_id, observed in eligible:
                runtime = self._runtime_by_task_id.get(task_id)
                if runtime != observed:
                    continue
                self._runtime_by_task_id[task_id] = _RuntimeTaskState(
                    status=self.TASK_QUEUED,
                    run_id=runtime.run_id,
                    judgehost_task_id=runtime.judgehost_task_id,
                    started_at="",
                )
                changed.append(task_id)
        return changed

    def _skip_pending_descendants(
        self,
        conn: sqlite3.Connection,
        *,
        verification_id: str,
        root_task_ids: set[str],
        feedback_text: str,
    ) -> set[str]:
        if not root_task_ids:
            return set()
        rows = conn.execute(
            """
            SELECT id, predecessor_task_id, final_status
            FROM verification_tasks
            WHERE verification_id=?
            """,
            [verification_id],
        ).fetchall()
        children_by_parent: dict[str, list[str]] = {}
        final_status_by_id: dict[str, str] = {}
        for row in rows:
            task_id = str(row["id"] or "")
            parent_id = str(row["predecessor_task_id"] or "")
            final_status_by_id[task_id] = str(row["final_status"] or "")
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(task_id)

        with self._runtime_lock.read_lock():
            active_task_ids = set(self._runtime_by_task_id)
        skipped: set[str] = set()
        stack = list(root_task_ids)
        while stack:
            parent_id = stack.pop()
            for child_id in children_by_parent.get(parent_id, []):
                stack.append(child_id)
                if child_id in active_task_ids or final_status_by_id.get(child_id, ""):
                    continue
                conn.execute(
                    """
                    UPDATE verification_tasks
                    SET final_status=?, verdict=?, feedback_text=?, finished_at=?
                    WHERE id=? AND final_status=''
                    """,
                    [
                        self.TASK_DONE,
                        "SK",
                        self._normalize_display_text(feedback_text),
                        now_iso(),
                        child_id,
                    ],
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    skipped.add(child_id)
                    final_status_by_id[child_id] = self.TASK_DONE
        return skipped

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
        answer_correct: bool = False,
    ) -> None:
        self.save_task_results(
            [
                {
                    "task_id": task_id,
                    "status": status,
                    "verdict": verdict,
                    "run_id": run_id,
                    "judgehost_task_id": judgehost_task_id,
                    "runtime_sec": runtime_sec,
                    "cpu_sec": cpu_sec,
                    "wall_sec": wall_sec,
                    "memory_kb": memory_kb,
                    "compile_log": compile_log,
                    "diagnostics_json": diagnostics_json,
                    "error_text": error_text,
                    "feedback_text": feedback_text,
                    "output_ref": output_ref,
                    "answer_correct": answer_correct,
                }
            ]
        )

    def save_task_results(self, results: list[dict[str, object]]) -> set[str]:
        if not results:
            return set()
        limit_bytes = self._limit_bytes()
        task_ids = [str(result["task_id"]) for result in results]
        with self._runtime_lock.read_lock():
            logical_run_ids = {
                task_id: self._logical_run_id_by_task_id.get(task_id, "")
                for task_id in task_ids
            }
        normalized_results = [
            {
                "task_id": str(result["task_id"]),
                "status": str(result["status"]),
                "verdict": str(result["verdict"]),
                "runtime_sec": result["runtime_sec"],
                "cpu_sec": result["cpu_sec"],
                "wall_sec": result["wall_sec"],
                "memory_kb": result["memory_kb"],
                "answer_correct": bool(result["answer_correct"]),
                "compile_log": bounded_display_text(str(result["compile_log"]), limit_bytes=limit_bytes),
                "diagnostics_json": normalize_diagnostics_json_text(
                    str(result["diagnostics_json"]),
                    message_limit=limit_bytes,
                ),
                "error_text": bounded_display_text(str(result["error_text"]), limit_bytes=limit_bytes),
                "feedback_text": bounded_display_text(str(result["feedback_text"]), limit_bytes=limit_bytes),
                "output_ref": str(result["output_ref"]),
            }
            for result in results
        ]
        skipped_task_ids: set[str] = set()

        def _tx(conn: sqlite3.Connection) -> None:
            rows = conn.execute(
                f"""
                SELECT id, verification_id, task_kind, test_name, final_status
                FROM verification_tasks
                WHERE id IN ({','.join('?' for _ in task_ids)})
                """,
                task_ids,
            ).fetchall()
            rows_by_id = {str(row["id"]): row for row in rows}
            owner_by_output_ref: dict[tuple[str, str], tuple[str, str]] = {}
            existing_owner_by_key: dict[tuple[str, str], tuple[str, str] | None] = {}
            effective_params: list[list[object]] = []
            content_duplicate_roots: set[str] = set()
            for result in normalized_results:
                task_id = str(result["task_id"])
                row = rows_by_id.get(task_id)
                if row is None:
                    continue
                verification_id = str(row["verification_id"] or "")
                task_kind = str(row["task_kind"] or "")
                output_ref = str(result["output_ref"])
                effective_verdict = str(result["verdict"])
                effective_feedback = str(result["feedback_text"])
                if (
                    task_kind == "generate-input"
                    and str(result["status"]) == self.TASK_DONE
                    and effective_verdict.upper() != "SK"
                    and output_ref
                ):
                    owner_key = (verification_id, output_ref)
                    owner = owner_by_output_ref.get(owner_key)
                    if owner is None:
                        if owner_key not in existing_owner_by_key:
                            owner_row = conn.execute(
                                """
                                SELECT id, test_name
                                FROM verification_tasks
                                WHERE verification_id=? AND task_kind='generate-input'
                                  AND final_status=? AND UPPER(verdict)<>? AND output_ref=?
                                ORDER BY finished_at ASC, id ASC
                                LIMIT 1
                                """,
                                [verification_id, self.TASK_DONE, "SK", output_ref],
                            ).fetchone()
                            existing_owner_by_key[owner_key] = (
                                None
                                if owner_row is None
                                else (str(owner_row["id"]), str(owner_row["test_name"] or ""))
                            )
                        owner = existing_owner_by_key[owner_key]
                    if owner is None:
                        owner_by_output_ref[owner_key] = (task_id, str(row["test_name"] or ""))
                    elif owner[0] != task_id:
                        effective_verdict = "SK"
                        effective_feedback = (
                            "duplicate generated input; skipped, same as "
                            f"{owner[1]}"
                        )
                        content_duplicate_roots.add(task_id)
                if (
                    task_kind == "generate-input"
                    and str(result["status"]) == self.TASK_DONE
                    and effective_verdict.upper() == "SK"
                ):
                    content_duplicate_roots.add(task_id)
                finished_at = (
                    now_iso()
                    if str(result["status"]) in {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED}
                    else None
                )
                effective_params.append(
                    [
                        result["status"],
                        effective_verdict,
                        result["runtime_sec"],
                        result["cpu_sec"],
                        result["wall_sec"],
                        result["memory_kb"],
                        1 if bool(result["answer_correct"]) else 0,
                        logical_run_ids[task_id],
                        result["compile_log"],
                        result["diagnostics_json"],
                        result["error_text"],
                        self._normalize_display_text(effective_feedback),
                        output_ref,
                        finished_at,
                        task_id,
                    ]
                )
            conn.executemany(
                """
                UPDATE verification_tasks
                SET final_status=?, verdict=?, runtime_sec=?, cpu_sec=?, wall_sec=?,
                    memory_kb=?, answer_correct=?, logical_run_id=?, compile_log=?,
                    diagnostics_json=?, error_text=?, feedback_text=?, output_ref=?,
                    finished_at=?
                WHERE id=? AND final_status=''
                """,
                effective_params,
            )
            for task_id in content_duplicate_roots:
                skipped_task_ids.add(task_id)
            for result in normalized_results:
                task_id = str(result["task_id"])
                row = rows_by_id.get(task_id)
                if row is None or str(row["task_kind"] or "") != "generate-input":
                    continue
                if str(result["status"]) == self.TASK_DONE and (
                    task_id in content_duplicate_roots or str(result["verdict"]).upper() == "SK"
                ):
                    skipped_task_ids.add(task_id)
            for verification_id in {
                str(rows_by_id[task_id]["verification_id"] or "")
                for task_id in skipped_task_ids
                if task_id in rows_by_id
            }:
                roots = {
                    task_id
                    for task_id in skipped_task_ids
                    if task_id in rows_by_id
                    and str(rows_by_id[task_id]["verification_id"] or "") == verification_id
                }
                skipped_task_ids.update(
                    self._skip_pending_descendants(
                        conn,
                        verification_id=verification_id,
                        root_task_ids=roots,
                        feedback_text="skipped because generate-input was skipped",
                    )
                )

        self.db.write_transaction(_tx)
        if skipped_task_ids:
            with self._runtime_lock.write_lock():
                for task_id in skipped_task_ids:
                    self._remove_judgehost_indexes_locked(task_id)
                    self._runtime_by_task_id.pop(task_id, None)
        return skipped_task_ids

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
        answer_correct: bool = False,
    ) -> None:
        with self._runtime_lock.read_lock():
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
            SET final_status=?, verdict=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?, answer_correct=?, logical_run_id=?, compile_log=?, diagnostics_json=?, error_text=?, feedback_text=?, output_ref=?, finished_at=COALESCE(?, finished_at)
            WHERE id=?
            """,
            [
                status,
                verdict,
                runtime_sec,
                cpu_sec,
                wall_sec,
                memory_kb,
                1 if bool(answer_correct) else 0,
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
    def overwrite_fail_reason(self, verification_id: str, *, reason: str) -> None:
        safe_reason = self._normalize_display_text(reason)
        self.db.execute(
            "UPDATE verifications SET fail_reason=? WHERE id=?",
            [safe_reason, verification_id],
        )
        with self._runtime_lock.write_lock():
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
        protected_judgehost_task_ids: set[str] | None = None,
    ) -> None:
        safe_reason = self._normalize_display_text(reason)
        protected = protected_judgehost_task_ids or set()
        for row in self.list_rows(verification_id):
            if str(row["status"] or "") not in {self.TASK_PENDING, self.TASK_QUEUED}:
                continue
            if str(row["judgehost_task_id"] or "") in protected:
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
        with self._runtime_lock.write_lock():
            if verification_id in self._fail_reason_by_verification_id:
                return
            self._fail_reason_by_verification_id[verification_id] = (
                self._normalize_display_text(reason)
            )

    def fail_state(self, verification_id: str) -> tuple[bool, str]:
        with self._runtime_lock.read_lock():
            fail_reason = self._fail_reason_by_verification_id.get(verification_id, "")
        return (bool(fail_reason), fail_reason)
