from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, replace
from typing import TypedDict, cast

from app.db import DB, now_iso
from app.service.platform.error_text import aux_display_text_limit_bytes, bounded_display_text
from app.service.platform.hashing import canonical_json
from app.service.platform.rwlock import WriterPriorityRWLock
from app.service.verification.execution_result import (
    ExecutionResult,
    execution_result_from_json,
    execution_result_json,
    execution_result_with_outcome,
    normalize_execution_result,
)
from app.service.verification.task_completion import (
    CompletionCommit,
    TaskCompletion,
    TaskCompletionAmendment,
)
from app.service.verification.task_metadata import canonical_diagnostics


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
    result: ExecutionResult
    result_json: str
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


def _diagnostics_from_text(text: str) -> tuple[dict[str, object], ...]:
    if not text:
        return ()
    return tuple(cast(list[dict[str, object]], json.loads(text)))


def _result_from_task_item(item: dict[str, object]) -> ExecutionResult:
    supplied = item.get("result")
    if isinstance(supplied, ExecutionResult):
        return supplied
    return normalize_execution_result(
        verdict=str(item.get("verdict") or ""),
        answer_correct=bool(item.get("answer_correct")),
        error=str(item.get("error_text") or ""),
        feedback=str(item.get("feedback_text") or ""),
        compile_log=str(item.get("compile_log") or ""),
        compile_diagnostics=_diagnostics_from_text(
            str(item.get("diagnostics_json") or "[]")
        ),
    )


def _bounded_result(result: ExecutionResult, *, limit_bytes: int) -> ExecutionResult:
    passes = tuple(
        replace(
            pass_result,
            feedback=bounded_display_text(
                pass_result.feedback,
                limit_bytes=limit_bytes,
            ),
        )
        for pass_result in result.passes
    )
    diagnostics = canonical_diagnostics(
        list(result.compile.diagnostics),
        list_limit=64,
        message_limit=limit_bytes,
    )["rows"]
    return normalize_execution_result(
        passes=passes,
        verdict=result.verdict,
        score_text=result.score_text,
        answer_correct=result.answer_correct,
        error=bounded_display_text(result.outcome.error, limit_bytes=limit_bytes),
        feedback=bounded_display_text(result.outcome.feedback, limit_bytes=limit_bytes),
        compile_log=bounded_display_text(result.compile.log, limit_bytes=limit_bytes),
        compile_diagnostics=diagnostics,
        warnings=(
            bounded_display_text(warning, limit_bytes=limit_bytes)
            for warning in result.warnings
        ),
    )


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
                        final_status,result_json,finished_at,created_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
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
                        execution_result_json(_result_from_task_item(item)),
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
        result_json = str(row["result_json"] or "{}")
        result = execution_result_from_json(result_json)
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
            "result": result,
            "result_json": result_json,
            "verdict": result.verdict,
            "run_id": runtime.run_id if runtime is not None else "",
            "judgehost_task_id": runtime.judgehost_task_id if runtime is not None else "",
            "runtime_sec": result.runtime_sec,
            "cpu_sec": result.cpu_sec,
            "wall_sec": result.wall_sec,
            "memory_kb": result.memory_kb,
            "answer_correct": result.answer_correct,
            "compile_log": result.compile.log,
            "diagnostics_json": canonical_json(
                list(result.compile.diagnostics),
                ensure_ascii=False,
            ),
            "error_text": result.outcome.error,
            "feedback_text": result.feedback_text,
            "output_ref": result.output_run_ref,
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
        result = execution_result_from_json(str(row["result_json"] or "{}"))
        return {
            "id": task_id,
            "verification_id": str(row["verification_id"] or ""),
            "task_kind": str(row["task_kind"] or ""),
            "source_path": str(row["source_path"] or ""),
            "logical_run_id": str(row["logical_run_id"] or runtime_logical_run_id),
            "test_name": str(row["test_name"] or ""),
            "expected_behavior": str(row["expected_behavior"] or ""),
            "status": status,
            "verdict": result.verdict,
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
                       test_name,expected_behavior,final_status,result_json
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
        active_task_ids: set[str],
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
                    SET final_status=?, result_json=?, finished_at=?
                    WHERE id=? AND final_status=''
                    """,
                    [
                        self.TASK_DONE,
                        execution_result_json(
                            normalize_execution_result(
                                verdict="SK",
                                feedback=self._normalize_display_text(feedback_text),
                            )
                        ),
                        now_iso(),
                        child_id,
                    ],
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    skipped.add(child_id)
                    final_status_by_id[child_id] = self.TASK_DONE
        return skipped

    def commit_task_completions(
        self,
        completions: list[TaskCompletion] | tuple[TaskCompletion, ...],
    ) -> CompletionCommit:
        if not completions:
            raise ValueError("task completion batch cannot be empty")
        task_ids = [completion.task_id for completion in completions]
        if any(not task_id for task_id in task_ids):
            raise ValueError("task completion id is required")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task completion batch contains a duplicate task id")
        terminal_statuses = {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED}
        if any(completion.status not in terminal_statuses for completion in completions):
            raise ValueError("task completion status must be terminal")
        normalized_by_id = {
            completion.task_id: replace(
                completion,
                result=_bounded_result(
                    completion.result,
                    limit_bytes=self._limit_bytes(),
                ),
                fail_reason=self._normalize_display_text(completion.fail_reason),
            )
            for completion in completions
        }

        with self._runtime_lock.write_lock():
            active_task_ids = set(self._runtime_by_task_id)

            def _tx(conn: sqlite3.Connection) -> tuple[CompletionCommit, str]:
                rows = conn.execute(
                    f"""
                    SELECT t.id,t.verification_id,t.task_kind,t.test_name,
                           t.final_status,t.result_json,
                           COALESCE(r.input_ref,'') AS input_ref,
                           COALESCE(r.answer_ref,'') AS answer_ref
                    FROM verification_tasks t
                    LEFT JOIN verification_artifact_refs r
                      ON r.verification_id=t.verification_id AND r.test_name=t.test_name
                    WHERE t.id IN ({','.join('?' for _ in task_ids)})
                    """,
                    task_ids,
                ).fetchall()
                rows_by_id = {str(row["id"]): row for row in rows}
                missing = [task_id for task_id in task_ids if task_id not in rows_by_id]
                if missing:
                    raise RuntimeError(
                        "unknown verification task completion: " + ", ".join(missing)
                    )
                verification_ids = {
                    str(row["verification_id"] or "") for row in rows
                }
                if len(verification_ids) != 1:
                    raise RuntimeError("task completion batch crosses verifications")
                verification_id = next(iter(verification_ids))
                owner_by_output_ref: dict[str, tuple[str, str]] = {}
                owner_rows = conn.execute(
                    """
                    SELECT id,test_name,result_json
                    FROM verification_tasks
                    WHERE verification_id=? AND task_kind='generate-input'
                      AND final_status=?
                    ORDER BY finished_at ASC,id ASC
                    """,
                    [verification_id, self.TASK_DONE],
                ).fetchall()
                for owner_row in owner_rows:
                    owner_result = execution_result_from_json(
                        str(owner_row["result_json"] or "{}")
                    )
                    output_ref = owner_result.output_run_ref
                    if output_ref and owner_result.verdict.upper() != "SK":
                        owner_by_output_ref.setdefault(
                            output_ref,
                            (
                                str(owner_row["id"]),
                                str(owner_row["test_name"] or ""),
                            ),
                        )

                effective: list[TaskCompletion] = []
                committed_task_ids: set[str] = set()
                already_terminal_task_ids: set[str] = set()
                skipped_task_ids: set[str] = set()
                stale_skipped_generator_ids: set[str] = set()
                for task_id in task_ids:
                    incoming = normalized_by_id[task_id]
                    row = rows_by_id[task_id]
                    current_status = str(row["final_status"] or "")
                    if current_status:
                        runtime = self._runtime_by_task_id.get(task_id)
                        current_result = execution_result_from_json(
                            str(row["result_json"] or "{}")
                        )
                        already_terminal_task_ids.add(task_id)
                        effective.append(
                            TaskCompletion(
                                task_id=task_id,
                                status=current_status,
                                run_id=(
                                    incoming.run_id
                                    if runtime is None
                                    else runtime.run_id
                                ),
                                judgehost_task_id=(
                                    incoming.judgehost_task_id
                                    if runtime is None
                                    else runtime.judgehost_task_id
                                ),
                                result=current_result,
                                input_ref=str(row["input_ref"] or ""),
                                answer_ref=str(row["answer_ref"] or ""),
                            )
                        )
                        if (
                            current_status == self.TASK_DONE
                            and current_result.verdict.upper() == "SK"
                        ):
                            skipped_task_ids.add(task_id)
                            if str(row["task_kind"] or "") == "generate-input":
                                stale_skipped_generator_ids.add(task_id)
                        continue

                    task_kind = str(row["task_kind"] or "")
                    result = incoming.result
                    output_ref = result.output_run_ref
                    if (
                        task_kind == "generate-input"
                        and incoming.status == self.TASK_DONE
                        and result.verdict.upper() != "SK"
                        and output_ref
                    ):
                        owner = owner_by_output_ref.get(output_ref)
                        if owner is None:
                            owner_by_output_ref[output_ref] = (
                                task_id,
                                str(row["test_name"] or ""),
                            )
                        elif owner[0] != task_id:
                            result = execution_result_with_outcome(
                                result,
                                verdict="SK",
                                feedback=self._normalize_display_text(
                                    "duplicate generated input; skipped, same as "
                                    f"{owner[1]}"
                                ),
                            )
                    effective_completion = replace(incoming, result=result)
                    conn.execute(
                        """
                        UPDATE verification_tasks
                        SET final_status=?,result_json=?,finished_at=?
                        WHERE id=? AND final_status=''
                        """,
                        [
                            effective_completion.status,
                            execution_result_json(effective_completion.result),
                            now_iso(),
                            task_id,
                        ],
                    )
                    if int(conn.execute("SELECT changes()").fetchone()[0]) != 1:
                        raise RuntimeError(
                            f"verification task {task_id} completion update was lost"
                        )
                    committed_task_ids.add(task_id)
                    effective.append(effective_completion)

                    if effective_completion.input_ref or effective_completion.answer_ref:
                        conn.execute(
                            """
                            INSERT INTO verification_artifact_refs(
                                verification_id,test_name,input_ref,answer_ref,updated_at
                            ) VALUES(?,?,?,?,?)
                            ON CONFLICT(verification_id,test_name) DO UPDATE SET
                                input_ref=CASE
                                    WHEN excluded.input_ref<>'' THEN excluded.input_ref
                                    ELSE verification_artifact_refs.input_ref
                                END,
                                answer_ref=CASE
                                    WHEN excluded.answer_ref<>'' THEN excluded.answer_ref
                                    ELSE verification_artifact_refs.answer_ref
                                END,
                                updated_at=excluded.updated_at
                            """,
                            [
                                verification_id,
                                str(row["test_name"] or ""),
                                effective_completion.input_ref,
                                effective_completion.answer_ref,
                                now_iso(),
                            ],
                        )
                    if effective_completion.fail_reason:
                        conn.execute(
                            """
                            UPDATE verifications
                            SET fail_reason=CASE
                                WHEN fail_reason='' THEN ? ELSE fail_reason
                            END
                            WHERE id=?
                            """,
                            [effective_completion.fail_reason, verification_id],
                        )
                    if (
                        task_kind == "generate-input"
                        and effective_completion.status == self.TASK_DONE
                        and effective_completion.result.verdict.upper() == "SK"
                    ):
                        skipped_task_ids.add(task_id)

                if skipped_task_ids:
                    skipped_task_ids.update(
                        self._skip_pending_descendants(
                            conn,
                            verification_id=verification_id,
                            root_task_ids=set(skipped_task_ids),
                            active_task_ids=active_task_ids,
                            feedback_text="skipped because generate-input was skipped",
                        )
                    )
                for root_task_id in stale_skipped_generator_ids:
                    durable_descendants = conn.execute(
                        """
                        WITH RECURSIVE descendants(id) AS (
                            SELECT ?
                            UNION
                            SELECT task.id
                            FROM verification_tasks task
                            JOIN descendants parent
                              ON task.predecessor_task_id=parent.id
                            WHERE task.verification_id=?
                        )
                        SELECT task.id,task.result_json
                        FROM verification_tasks task
                        JOIN descendants ON descendants.id=task.id
                        WHERE task.id<>? AND task.final_status=?
                        """,
                        [
                            root_task_id,
                            verification_id,
                            root_task_id,
                            self.TASK_DONE,
                        ],
                    ).fetchall()
                    skipped_task_ids.update(
                        str(item["id"])
                        for item in durable_descendants
                        if execution_result_from_json(
                            str(item["result_json"] or "{}")
                        ).verdict.upper()
                        == "SK"
                    )
                fail_row = conn.execute(
                    "SELECT fail_reason FROM verifications WHERE id=?",
                    [verification_id],
                ).fetchone()
                fail_reason = "" if fail_row is None else str(fail_row["fail_reason"] or "")
                return (
                    CompletionCommit(
                        verification_id=verification_id,
                        effective_completions=tuple(effective),
                        committed_task_ids=frozenset(committed_task_ids),
                        already_terminal_task_ids=frozenset(already_terminal_task_ids),
                        skipped_task_ids=frozenset(skipped_task_ids),
                    ),
                    fail_reason,
                )

            commit, fail_reason = self.db.write_transaction(_tx)
            if fail_reason:
                self._fail_reason_by_verification_id[
                    commit.verification_id
                ] = fail_reason
            else:
                self._fail_reason_by_verification_id.pop(
                    commit.verification_id,
                    None,
                )
            for task_id in commit.skipped_task_ids:
                self._remove_judgehost_indexes_locked(task_id)
                self._runtime_by_task_id.pop(task_id, None)
            return commit

    def amend_task_completion(
        self,
        amendment: TaskCompletionAmendment,
    ) -> CompletionCommit | None:
        safe_error = self._normalize_display_text(amendment.error_text)
        safe_feedback = self._normalize_display_text(amendment.feedback_text)
        fail_reason_origin = self._normalize_display_text(
            amendment.fail_reason_origin
        )
        expected_fail_reason = self._normalize_display_text(
            amendment.expected_fail_reason
        )
        replacement_fail_reason = self._normalize_display_text(
            amendment.fail_reason
        )
        with self._runtime_lock.write_lock():
            def _tx(
                conn: sqlite3.Connection,
            ) -> tuple[CompletionCommit | None, str]:
                row = conn.execute(
                    """
                    SELECT t.verification_id,t.final_status,t.result_json,
                           COALESCE(r.input_ref,'') AS input_ref,
                           COALESCE(r.answer_ref,'') AS answer_ref
                    FROM verification_tasks t
                    LEFT JOIN verification_artifact_refs r
                      ON r.verification_id=t.verification_id
                     AND r.test_name=t.test_name
                    WHERE t.id=?
                    """,
                    [amendment.task_id],
                ).fetchone()
                if row is None or not str(row["final_status"] or ""):
                    return (None, "")
                verification_id = str(row["verification_id"] or "")
                current_result = execution_result_from_json(
                    str(row["result_json"] or "{}")
                )
                amended_result = _bounded_result(
                    execution_result_with_outcome(
                        current_result,
                        error=safe_error,
                        feedback=safe_feedback,
                    ),
                    limit_bytes=self._limit_bytes(),
                )
                fail_row = conn.execute(
                    "SELECT fail_reason FROM verifications WHERE id=?",
                    [verification_id],
                ).fetchone()
                current_fail_reason = (
                    "" if fail_row is None else str(fail_row["fail_reason"] or "")
                )
                same_task_fail_reason_changed = (
                    replacement_fail_reason
                    and current_fail_reason
                    and current_fail_reason != expected_fail_reason
                    and fail_reason_origin
                    and (
                        current_fail_reason == fail_reason_origin
                        or current_fail_reason.startswith(
                            f"{fail_reason_origin}:"
                        )
                    )
                )
                if same_task_fail_reason_changed:
                    return (None, current_fail_reason)
                conn.execute(
                    "UPDATE verification_tasks SET result_json=? WHERE id=?",
                    [execution_result_json(amended_result), amendment.task_id],
                )
                if replacement_fail_reason and (
                    not current_fail_reason
                    or current_fail_reason == expected_fail_reason
                ):
                    conn.execute(
                        "UPDATE verifications SET fail_reason=? WHERE id=?",
                        [replacement_fail_reason, verification_id],
                    )
                    current_fail_reason = replacement_fail_reason
                runtime = self._runtime_by_task_id.get(amendment.task_id)
                completion = TaskCompletion(
                    task_id=amendment.task_id,
                    status=str(row["final_status"] or ""),
                    run_id="" if runtime is None else runtime.run_id,
                    judgehost_task_id=(
                        "" if runtime is None else runtime.judgehost_task_id
                    ),
                    result=amended_result,
                    input_ref=str(row["input_ref"] or ""),
                    answer_ref=str(row["answer_ref"] or ""),
                )
                skipped_task_ids = (
                    frozenset({amendment.task_id})
                    if amended_result.verdict.upper() == "SK"
                    else frozenset()
                )
                return (
                    CompletionCommit(
                        verification_id=verification_id,
                        effective_completions=(completion,),
                        committed_task_ids=frozenset(),
                        already_terminal_task_ids=frozenset(
                            {amendment.task_id}
                        ),
                        skipped_task_ids=skipped_task_ids,
                    ),
                    current_fail_reason,
                )

            commit, effective_fail_reason = self.db.write_transaction(_tx)
            if commit is None:
                return None
            if effective_fail_reason:
                self._fail_reason_by_verification_id[
                    commit.verification_id
                ] = effective_fail_reason
            else:
                self._fail_reason_by_verification_id.pop(
                    commit.verification_id,
                    None,
                )
            return commit

    def cancel_unfinished_tasks(self, verification_id: str, *, reason: str) -> None:
        finished_at = now_iso()
        safe_reason = self._normalize_display_text(reason)
        self.set_fail_flag(verification_id, reason=safe_reason)
        for row in self.list_rows(verification_id):
            if str(row["status"] or "") in {self.TASK_DONE, self.TASK_FAILED, self.TASK_CANCELLED}:
                continue
            self.commit_task_completions(
                [
                    TaskCompletion(
                        task_id=str(row["id"]),
                        status=self.TASK_CANCELLED,
                        run_id=str(row["run_id"] or ""),
                        judgehost_task_id=str(row["judgehost_task_id"] or ""),
                        result=normalize_execution_result(error=safe_reason),
                    )
                ]
            )
        self.db.execute(
            "UPDATE verifications SET finished_at=COALESCE(finished_at, ?) WHERE id=?",
            [finished_at, verification_id],
        )

    def cancel_not_started_tasks(
        self,
        verification_id: str,
        *,
        reason: str,
        protected_judgehost_task_ids: set[str] | None = None,
    ) -> None:
        safe_reason = self._normalize_display_text(reason)
        self.set_fail_flag(verification_id, reason=safe_reason)
        protected = protected_judgehost_task_ids or set()
        for row in self.list_rows(verification_id):
            if str(row["status"] or "") not in {self.TASK_PENDING, self.TASK_QUEUED}:
                continue
            if str(row["judgehost_task_id"] or "") in protected:
                continue
            self.commit_task_completions(
                [
                    TaskCompletion(
                        task_id=str(row["id"]),
                        status=self.TASK_CANCELLED,
                        run_id=str(row["run_id"] or ""),
                        judgehost_task_id=str(row["judgehost_task_id"] or ""),
                        result=normalize_execution_result(error=safe_reason),
                    )
                ]
            )

    def set_fail_flag(self, verification_id: str, *, reason: str) -> None:
        safe_reason = self._normalize_display_text(reason)
        if not safe_reason:
            return
        with self._runtime_lock.write_lock():
            def _tx(conn: sqlite3.Connection) -> str:
                conn.execute(
                    """
                    UPDATE verifications
                    SET fail_reason=CASE
                        WHEN fail_reason='' THEN ? ELSE fail_reason
                    END
                    WHERE id=?
                    """,
                    [safe_reason, verification_id],
                )
                row = conn.execute(
                    "SELECT fail_reason FROM verifications WHERE id=?",
                    [verification_id],
                ).fetchone()
                return "" if row is None else str(row["fail_reason"] or "")

            effective_reason = self.db.write_transaction(_tx)
            if effective_reason:
                self._fail_reason_by_verification_id[
                    verification_id
                ] = effective_reason
            else:
                self._fail_reason_by_verification_id.pop(
                    verification_id,
                    None,
                )

    def fail_state(self, verification_id: str) -> tuple[bool, str]:
        with self._runtime_lock.read_lock():
            fail_reason = self._fail_reason_by_verification_id.get(verification_id, "")
        return (bool(fail_reason), fail_reason)

    def reset_runtime_state(self) -> None:
        """Forget all process-local indexes after exclusive artifact cleanup."""

        with self._runtime_lock.write_lock():
            self._runtime_by_task_id.clear()
            self._logical_run_id_by_task_id.clear()
            self._test_name_by_task_id.clear()
            self._task_id_by_judgehost_case.clear()
            self._task_ids_by_judgehost_task.clear()
            self._fail_reason_by_verification_id.clear()
