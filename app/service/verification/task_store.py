from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TypeVar, TypedDict, cast

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
from app.service.verification.diagnostic import (
    DiagnosticMergeOutcome,
    TaskDiagnosticSnapshot,
    compose_task_diagnostic_display,
    merge_task_diagnostic_snapshot,
    new_task_diagnostic_item,
    task_diagnostic_snapshot_from_json,
    task_diagnostic_snapshot_json,
)
from app.service.verification.lifecycle import (
    ActivationCommit,
    ActivationPlan,
    ParentTransition,
    SanityFinish,
    StartupRecoverySummary,
    VerificationTransitionCommit,
    cancelled_task_result,
)
from app.service.verification.task_completion import (
    CompletionCommit,
    TaskCompletion,
)
from app.service.verification.task_metadata import canonical_diagnostics


class VerificationTaskRow(TypedDict):
    id: str
    verification_id: str
    predecessor_task_id: str
    task_kind: str
    source_path: str
    program_id: str
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
    program_id: str
    test_name: str
    expected_behavior: str
    status: str
    verdict: str


class VerificationTaskReadRow(TypedDict):
    id: str
    task_kind: str
    source_path: str
    program_id: str
    test_name: str
    status: str


@dataclass
class _RuntimeTaskState:
    status: str
    run_id: str
    judgehost_task_id: str
    started_at: str


_SnapshotValue = TypeVar("_SnapshotValue")
_HARD_FAILURE_TASK_KINDS = frozenset(("generate-input", "main-correct"))


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
        self._test_name_by_task_id: dict[str, str] = {}

    @staticmethod
    def _limit_bytes() -> int:
        return aux_display_text_limit_bytes()

    @classmethod
    def _normalize_display_text(cls, value: str) -> str:
        return bounded_display_text(value, limit_bytes=cls._limit_bytes())

    def run_problem_deletion(
        self,
        problem_id: int,
        *,
        delete_metadata: Callable[
            [sqlite3.Connection],
            _SnapshotValue,
        ],
    ) -> _SnapshotValue:
        """Delete one problem under the verification lifecycle lock order."""

        deleted_task_ids: tuple[str, ...] = ()
        with self._runtime_lock.write_lock():
            def _tx(conn: sqlite3.Connection) -> _SnapshotValue:
                nonlocal deleted_task_ids
                rows = conn.execute(
                    """
                    SELECT task.id
                    FROM verification_tasks task
                    JOIN verifications verification
                      ON verification.id=task.verification_id
                    WHERE verification.problem_id=?
                    """,
                    [int(problem_id)],
                ).fetchall()
                deleted_task_ids = tuple(
                    str(row["id"] or "")
                    for row in rows
                    if str(row["id"] or "")
                )
                if any(
                    task_id in self._runtime_by_task_id
                    for task_id in deleted_task_ids
                ):
                    raise ValueError(
                        "cannot delete problem while verification runtime is draining"
                    )
                return delete_metadata(conn)

            result = self.db.write_transaction(_tx)
            for task_id in deleted_task_ids:
                self._test_name_by_task_id.pop(task_id, None)
            return result

    def activate_plan(
        self,
        plan: ActivationPlan,
        *,
        write_detail: Callable[
            [sqlite3.Connection, str, dict[str, object]],
            None,
        ],
    ) -> ActivationCommit:
        ordered_tasks = plan.ordered_tasks()
        detail = plan.detail()
        now_text = now_iso()
        test_names = {task.task_id: task.test_name for task in ordered_tasks}

        with self._runtime_lock.write_lock():
            def _tx(conn: sqlite3.Connection) -> ActivationCommit:
                cursor = conn.execute(
                    """
                    UPDATE verifications
                    SET status='running',finished_at=NULL
                    WHERE id=? AND status='queued'
                    """,
                    [plan.verification_id],
                )
                if int(cursor.rowcount or 0) != 1:
                    row = conn.execute(
                        "SELECT status FROM verifications WHERE id=?",
                        [plan.verification_id],
                    ).fetchone()
                    if row is None:
                        outcome = "missing"
                    elif str(row["status"] or "") == "running":
                        outcome = "already-running"
                    else:
                        outcome = "closed"
                    return ActivationCommit(
                        verification_id=plan.verification_id,
                        outcome=outcome,
                    )
                write_detail(conn, plan.verification_id, detail)
                conn.executemany(
                    """
                    INSERT INTO verification_tasks(
                        id,verification_id,predecessor_task_id,task_kind,source_path,program_id,test_name,expected_behavior,
                        final_status,result_json,finished_at,created_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            task.task_id,
                            plan.verification_id,
                            task.predecessor_task_id,
                            task.task_kind,
                            task.source_path,
                            task.program_id,
                            task.test_name,
                            task.expected_behavior,
                            "",
                            execution_result_json(
                                _bounded_result(
                                    task.result,
                                    limit_bytes=self._limit_bytes(),
                                )
                            ),
                            None,
                            now_text,
                        )
                        for task in ordered_tasks
                    ],
                )
                return ActivationCommit(
                    verification_id=plan.verification_id,
                    outcome="activated",
                    task_ids=tuple(task.task_id for task in ordered_tasks),
                )

            commit = self.db.write_transaction(_tx)
            if commit.outcome != "activated":
                return commit
            self._test_name_by_task_id.update(test_names)
            return commit

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
        task_id = str(row["id"] or "")
        with self._runtime_lock.read_lock():
            runtime = self._runtime_by_task_id.get(task_id)
        return self._decorate_row_with_runtime(
            index,
            row,
            runtime=runtime,
        )

    def _decorate_row_with_runtime(
        self,
        index: int,
        row: dict[str, object],
        *,
        runtime: _RuntimeTaskState | None,
    ) -> VerificationTaskRow:
        verification_id = str(row["verification_id"] or "")
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
        program_id = str(row["program_id"] or "")
        result_json = str(row["result_json"] or "{}")
        result = execution_result_from_json(result_json)
        return {
            "id": task_id,
            "verification_id": verification_id,
            "predecessor_task_id": str(row["predecessor_task_id"] or ""),
            "task_kind": task_kind,
            "source_path": source_path,
            "program_id": program_id,
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
        result = execution_result_from_json(str(row["result_json"] or "{}"))
        return {
            "id": task_id,
            "verification_id": str(row["verification_id"] or ""),
            "task_kind": str(row["task_kind"] or ""),
            "source_path": str(row["source_path"] or ""),
            "program_id": str(row["program_id"] or ""),
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
                SELECT id,verification_id,task_kind,source_path,program_id,
                       test_name,expected_behavior,final_status,result_json
                FROM verification_tasks
                WHERE verification_id=?
                """,
                [verification_id],
            )
        ]
        ordered = sorted(rows, key=self._row_order)
        return [self._decorate_list_row(row) for row in ordered]

    def snapshot_rows(
        self,
        conn: sqlite3.Connection,
        verification_id: str,
    ) -> list[dict[str, object]]:
        rows = conn.execute(
            """
            SELECT task.*,
                   COALESCE(ref.input_ref,'') AS input_ref,
                   COALESCE(ref.answer_ref,'') AS answer_ref,
                   COALESCE(diagnostic.snapshot_json,'') AS late_diagnostic_json
            FROM verification_tasks task
            LEFT JOIN verification_artifact_refs ref
              ON ref.verification_id=task.verification_id
             AND ref.test_name=task.test_name
            LEFT JOIN verification_task_diagnostics diagnostic
              ON diagnostic.task_id=task.id
            WHERE task.verification_id=?
            """,
            [verification_id],
        ).fetchall()
        ordered = sorted((dict(row) for row in rows), key=self._row_order)
        values: list[dict[str, object]] = []
        for index, row in enumerate(ordered, start=1):
            task_id = str(row["id"] or "")
            decorated = dict(
                self._decorate_row_with_runtime(
                    index,
                    row,
                    runtime=self._runtime_by_task_id.get(task_id),
                )
            )
            snapshot = task_diagnostic_snapshot_from_json(
                str(row["late_diagnostic_json"] or "")
            )
            decorated["input_ref"] = str(row["input_ref"] or "")
            decorated["answer_ref"] = str(row["answer_ref"] or "")
            display = compose_task_diagnostic_display(
                cast(ExecutionResult, decorated["result"]),
                snapshot,
            )
            decorated["late_diagnostics"] = display["late_diagnostics"]
            decorated["late_diagnostic_text"] = display["late_text"]
            decorated["diagnostic_display"] = display
            values.append(decorated)
        return values

    def read_lifecycle_snapshot(
        self,
        reader: Callable[[sqlite3.Connection], _SnapshotValue],
    ) -> _SnapshotValue:
        """Read SQLite and runtime overlays under the lifecycle lock order."""

        with self._runtime_lock.read_lock():
            with self.db.conn() as conn:
                conn.execute("BEGIN")
                return reader(conn)

    def runtime_row(self, task_id: str) -> VerificationTaskRow | None:
        if not task_id:
            return None
        with self._runtime_lock.read_lock():
            runtime = self._runtime_by_task_id.get(task_id)
            if runtime is None:
                return None
            with self.db.conn() as conn:
                conn.execute("BEGIN")
                row = conn.execute(
                    "SELECT * FROM verification_tasks WHERE id=?",
                    [task_id],
                ).fetchone()
            if row is None:
                return None
            return self._decorate_row_with_runtime(
                1,
                dict(row),
                runtime=runtime,
            )

    def bind_and_expose_judgehost_runtime(
        self,
        verification_task_id: str,
        *,
        run_id: str,
        judgehost_task_id: str,
        expose: Callable[[], None],
    ) -> bool:
        if not verification_task_id or not run_id or not judgehost_task_id:
            raise ValueError("verification Judgehost binding identities are required")
        with self._runtime_lock.write_lock():
            row = self.db.fetch_one(
                """
                SELECT task.final_status,verification.status AS verification_status
                FROM verification_tasks task
                JOIN verifications verification ON verification.id=task.verification_id
                WHERE task.id=?
                """,
                [verification_task_id],
            )
            if (
                row is None
                or str(row["final_status"] or "")
                or str(row["verification_status"] or "") != "running"
            ):
                return False
            current = self._runtime_by_task_id.get(verification_task_id)
            if current is not None:
                if (
                    current.run_id != run_id
                    or current.judgehost_task_id != judgehost_task_id
                ):
                    return False
                expose()
                return True
            runtime = _RuntimeTaskState(
                status=self.TASK_QUEUED,
                run_id=run_id,
                judgehost_task_id=judgehost_task_id,
                started_at="",
            )
            self._runtime_by_task_id[verification_task_id] = runtime
            try:
                expose()
            except Exception:
                self._runtime_by_task_id.pop(verification_task_id, None)
                raise
            return True

    def unbind_judgehost_runtime(
        self,
        verification_task_id: str,
        *,
        judgehost_task_id: str,
    ) -> bool:
        with self._runtime_lock.write_lock():
            current = self._runtime_by_task_id.get(verification_task_id)
            if current is None or current.judgehost_task_id != judgehost_task_id:
                return False
            self._runtime_by_task_id.pop(verification_task_id, None)
            return True

    def set_task_leased(self, task_id: str) -> bool:
        with self._runtime_lock.write_lock():
            current = self._runtime_by_task_id.get(task_id)
            if current is None:
                return False
            with self.db.conn() as conn:
                conn.execute("BEGIN")
                row = conn.execute(
                    """
                    SELECT task.final_status,
                           verification.status AS verification_status
                    FROM verification_tasks task
                    JOIN verifications verification
                      ON verification.id=task.verification_id
                    WHERE task.id=?
                    """,
                    [task_id],
                ).fetchone()
            if (
                row is None
                or str(row["final_status"] or "")
                or str(row["verification_status"] or "") != "running"
            ):
                return False
            self._runtime_by_task_id[task_id] = _RuntimeTaskState(
                status=self.TASK_LEASED,
                run_id=current.run_id,
                judgehost_task_id=current.judgehost_task_id,
                started_at=current.started_at or now_iso(),
            )
            return True

    def requeue_leased_tasks(
        self,
        verification_id: str,
        judgehost_task_ids: list[str],
    ) -> list[str]:
        allowed = {str(task_id) for task_id in judgehost_task_ids if str(task_id)}
        if not allowed:
            return []
        changed: list[str] = []
        with self._runtime_lock.write_lock():
            candidates = [
                (task_id, runtime)
                for task_id, runtime in self._runtime_by_task_id.items()
                if runtime.status == self.TASK_LEASED
                and runtime.judgehost_task_id in allowed
            ]
            with self.db.conn() as conn:
                conn.execute("BEGIN")
                for task_id, runtime in candidates:
                    row = conn.execute(
                        """
                        SELECT task.verification_id,task.final_status,
                               verification.status AS verification_status
                        FROM verification_tasks task
                        JOIN verifications verification
                          ON verification.id=task.verification_id
                        WHERE task.id=?
                        """,
                        [task_id],
                    ).fetchone()
                    if (
                        row is None
                        or str(row["verification_id"] or "") != verification_id
                        or str(row["final_status"] or "")
                        or str(row["verification_status"] or "") != "running"
                    ):
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

    def _cancel_open_tasks(
        self,
        conn: sqlite3.Connection,
        *,
        verification_id: str,
        reason: str,
        finished_at: str,
    ) -> set[str]:
        rows = conn.execute(
            """
            SELECT id
            FROM verification_tasks
            WHERE verification_id=? AND final_status=''
            ORDER BY created_at ASC,id ASC
            """,
            [verification_id],
        ).fetchall()
        task_ids = {str(row["id"] or "") for row in rows}
        task_ids.discard("")
        if not task_ids:
            return set()
        conn.execute(
            """
            UPDATE verification_tasks
            SET final_status=?,result_json=?,finished_at=?
            WHERE verification_id=? AND final_status=''
            """,
            [
                self.TASK_CANCELLED,
                execution_result_json(cancelled_task_result(reason)),
                finished_at,
                verification_id,
            ],
        )
        return task_ids

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
        if any(
            completion.status in {self.TASK_FAILED, self.TASK_CANCELLED}
            and not completion.fail_reason
            for completion in normalized_by_id.values()
        ):
            raise ValueError("failed or cancelled task completion needs a reason")

        with self._runtime_lock.write_lock():
            active_task_ids = set(self._runtime_by_task_id)

            def _tx(conn: sqlite3.Connection) -> CompletionCommit:
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
                cancelled_task_ids: set[str] = set()
                stale_skipped_generator_ids: set[str] = set()
                new_failure_reason = ""
                hard_failure_reason = ""
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
                    if effective_completion.fail_reason and not new_failure_reason:
                        new_failure_reason = effective_completion.fail_reason
                    if (
                        effective_completion.fail_reason
                        and not hard_failure_reason
                        and (
                            effective_completion.status == self.TASK_CANCELLED
                            or task_kind in _HARD_FAILURE_TASK_KINDS
                        )
                    ):
                        hard_failure_reason = effective_completion.fail_reason

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
                parent_transition: ParentTransition = ""
                sanity_claimed = False
                if new_failure_reason:
                    conn.execute(
                        """
                        UPDATE verifications
                        SET fail_reason=CASE
                            WHEN fail_reason='' THEN ? ELSE fail_reason
                        END
                        WHERE id=? AND status='running'
                        """,
                        [new_failure_reason, verification_id],
                    )
                if hard_failure_reason:
                    cursor = conn.execute(
                        """
                        UPDATE verifications
                        SET status='failed',
                            sanity_status=CASE
                                WHEN sanity_status IN ('pending','running') THEN 'skipped'
                                ELSE sanity_status
                            END,
                            finished_at=COALESCE(finished_at,?)
                        WHERE id=? AND status='running'
                        """,
                        [now_iso(), verification_id],
                    )
                    if int(cursor.rowcount or 0) == 1:
                        parent_transition = "failed"
                    parent_row = conn.execute(
                        "SELECT fail_reason FROM verifications WHERE id=?",
                        [verification_id],
                    ).fetchone()
                    effective_reason = (
                        hard_failure_reason
                        if parent_row is None
                        else str(parent_row["fail_reason"] or hard_failure_reason)
                    )
                    cancelled_task_ids.update(
                        self._cancel_open_tasks(
                            conn,
                            verification_id=verification_id,
                            reason=effective_reason,
                            finished_at=now_iso(),
                        )
                    )
                if not hard_failure_reason:
                    open_row = conn.execute(
                        """
                        SELECT id FROM verification_tasks
                        WHERE verification_id=? AND final_status=''
                        LIMIT 1
                        """,
                        [verification_id],
                    ).fetchone()
                    if open_row is None:
                        parent_row = conn.execute(
                            """
                            SELECT status,sanity_status,fail_reason
                            FROM verifications WHERE id=?
                            """,
                            [verification_id],
                        ).fetchone()
                        if (
                            parent_row is not None
                            and str(parent_row["status"] or "") == "running"
                        ):
                            sanity_status = str(parent_row["sanity_status"] or "")
                            parent_failure_reason = str(
                                parent_row["fail_reason"] or ""
                            )
                            if parent_failure_reason:
                                cursor = conn.execute(
                                    """
                                    UPDATE verifications
                                    SET status='failed',
                                        sanity_status=CASE
                                            WHEN sanity_status IN ('pending','running')
                                            THEN 'skipped'
                                            ELSE sanity_status
                                        END,
                                        finished_at=COALESCE(finished_at,?)
                                    WHERE id=? AND status='running'
                                    """,
                                    [now_iso(), verification_id],
                                )
                                if int(cursor.rowcount or 0) == 1:
                                    parent_transition = "failed"
                            elif sanity_status == "pending":
                                cursor = conn.execute(
                                    """
                                    UPDATE verifications SET sanity_status='running'
                                    WHERE id=? AND status='running'
                                      AND sanity_status='pending'
                                    """,
                                    [verification_id],
                                )
                                sanity_claimed = int(cursor.rowcount or 0) == 1
                                if sanity_claimed:
                                    parent_transition = "sanity-running"
                            elif sanity_status != "running":
                                cursor = conn.execute(
                                    """
                                    UPDATE verifications
                                    SET status='ok',finished_at=?
                                    WHERE id=? AND status='running'
                                    """,
                                    [now_iso(), verification_id],
                                )
                                if int(cursor.rowcount or 0) == 1:
                                    parent_transition = "ok"
                fail_row = conn.execute(
                    "SELECT fail_reason FROM verifications WHERE id=?",
                    [verification_id],
                ).fetchone()
                failure_reason = (
                    "" if fail_row is None else str(fail_row["fail_reason"] or "")
                )
                return CompletionCommit(
                    verification_id=verification_id,
                    effective_completions=tuple(effective),
                    committed_task_ids=frozenset(committed_task_ids),
                    already_terminal_task_ids=frozenset(already_terminal_task_ids),
                    skipped_task_ids=frozenset(skipped_task_ids),
                    cancelled_task_ids=frozenset(cancelled_task_ids),
                    parent_transition=parent_transition,
                    sanity_claimed=sanity_claimed,
                    failure_reason=failure_reason,
                )

            return self.db.write_transaction(_tx)

    def transition_verification_failed(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationTransitionCommit:
        safe_reason = self._normalize_display_text(
            reason or "verification cancelled by user"
        )
        finished_at = now_iso()
        with self._runtime_lock.write_lock():
            def _tx(conn: sqlite3.Connection) -> VerificationTransitionCommit:
                row = conn.execute(
                    "SELECT status FROM verifications WHERE id=?",
                    [verification_id],
                ).fetchone()
                if row is None:
                    return VerificationTransitionCommit(
                        verification_id=verification_id,
                        outcome="missing",
                        status="",
                    )
                current_status = str(row["status"] or "")
                if current_status not in {"queued", "running"}:
                    return VerificationTransitionCommit(
                        verification_id=verification_id,
                        outcome="closed",
                        status=current_status,
                    )
                cursor = conn.execute(
                    """
                    UPDATE verifications
                    SET status='failed',
                        fail_reason=CASE
                            WHEN fail_reason='' THEN ? ELSE fail_reason
                        END,
                        sanity_status=CASE
                            WHEN sanity_status IN ('pending','running') THEN 'skipped'
                            ELSE sanity_status
                        END,
                        finished_at=COALESCE(finished_at,?)
                    WHERE id=? AND status IN ('queued','running')
                    """,
                    [safe_reason, finished_at, verification_id],
                )
                if int(cursor.rowcount or 0) != 1:
                    raise RuntimeError(
                        f"verification {verification_id} failure transition was lost"
                    )
                reason_row = conn.execute(
                    "SELECT fail_reason FROM verifications WHERE id=?",
                    [verification_id],
                ).fetchone()
                effective_reason = (
                    safe_reason
                    if reason_row is None
                    else str(reason_row["fail_reason"] or safe_reason)
                )
                cancelled = self._cancel_open_tasks(
                    conn,
                    verification_id=verification_id,
                    reason=effective_reason,
                    finished_at=finished_at,
                )
                return VerificationTransitionCommit(
                    verification_id=verification_id,
                    outcome="transitioned",
                    status="failed",
                    cancelled_task_ids=frozenset(cancelled),
                )

            return self.db.write_transaction(_tx)

    def finish_sanity(
        self,
        finish: SanityFinish,
        *,
        write_detail: Callable[
            [sqlite3.Connection, str, dict[str, object]],
            None,
        ],
    ) -> VerificationTransitionCommit:
        detail = finish.detail()
        with self._runtime_lock.write_lock():
            def _tx(conn: sqlite3.Connection) -> VerificationTransitionCommit:
                cursor = conn.execute(
                    """
                    UPDATE verifications
                    SET status='ok',finished_at=?
                    WHERE id=? AND status='running' AND sanity_status='running'
                      AND fail_reason=''
                      AND NOT EXISTS (
                          SELECT 1 FROM verification_tasks
                          WHERE verification_id=? AND final_status=''
                      )
                    """,
                    [
                        now_iso(),
                        finish.verification_id,
                        finish.verification_id,
                    ],
                )
                if int(cursor.rowcount or 0) == 1:
                    write_detail(conn, finish.verification_id, detail)
                    return VerificationTransitionCommit(
                        verification_id=finish.verification_id,
                        outcome="transitioned",
                        status="ok",
                    )
                row = conn.execute(
                    "SELECT status FROM verifications WHERE id=?",
                    [finish.verification_id],
                ).fetchone()
                if row is None:
                    return VerificationTransitionCommit(
                        verification_id=finish.verification_id,
                        outcome="missing",
                        status="",
                    )
                return VerificationTransitionCommit(
                    verification_id=finish.verification_id,
                    outcome="closed",
                    status=str(row["status"] or ""),
                )

            return self.db.write_transaction(_tx)

    def append_diagnostic(
        self,
        *,
        task_id: str,
        kind: str,
        hostname: str,
        text: str,
        received_at: str,
    ) -> DiagnosticMergeOutcome:
        now_text = now_iso()
        item = new_task_diagnostic_item(
            kind=kind,
            hostname=hostname,
            text=text,
            received_at=received_at,
            limit_bytes=self._limit_bytes(),
        )
        with self._runtime_lock.write_lock():
            if task_id not in self._runtime_by_task_id:
                return "not-applicable"

            def _tx(conn: sqlite3.Connection) -> DiagnosticMergeOutcome:
                row = conn.execute(
                    """
                    SELECT task.final_status,diagnostic.snapshot_json
                    FROM verification_tasks task
                    LEFT JOIN verification_task_diagnostics diagnostic
                      ON diagnostic.task_id=task.id
                    WHERE task.id=?
                    """,
                    [task_id],
                ).fetchone()
                if row is None or not str(row["final_status"] or ""):
                    return "not-applicable"
                snapshot = task_diagnostic_snapshot_from_json(
                    str(row["snapshot_json"] or "")
                )
                merged, outcome = merge_task_diagnostic_snapshot(
                    snapshot,
                    item,
                    limit_bytes=self._limit_bytes(),
                )
                if outcome != "persisted":
                    return outcome
                conn.execute(
                    """
                    INSERT INTO verification_task_diagnostics(
                        task_id,snapshot_json,updated_at
                    ) VALUES(?,?,?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        snapshot_json=excluded.snapshot_json,
                        updated_at=excluded.updated_at
                    """,
                    [task_id, task_diagnostic_snapshot_json(merged), now_text],
                )
                return outcome

            return self.db.write_transaction(_tx)

    def diagnostic_snapshot(self, task_id: str) -> TaskDiagnosticSnapshot:
        row = self.db.fetch_one(
            "SELECT snapshot_json FROM verification_task_diagnostics WHERE task_id=?",
            [task_id],
        )
        if row is None:
            return TaskDiagnosticSnapshot()
        return task_diagnostic_snapshot_from_json(str(row["snapshot_json"] or ""))

    def recover_startup(self, *, reason: str) -> StartupRecoverySummary:
        safe_reason = self._normalize_display_text(
            reason or "cancelled on service startup"
        )
        finished_at = now_iso()
        with self._runtime_lock.write_lock():
            def _tx(conn: sqlite3.Connection) -> StartupRecoverySummary:
                verification_rows = conn.execute(
                    """
                    SELECT id FROM verifications
                    WHERE status IN ('queued','running')
                    ORDER BY created_at ASC,id ASC
                    """
                ).fetchall()
                verification_ids = tuple(
                    str(row["id"] or "") for row in verification_rows
                    if str(row["id"] or "")
                )
                if not verification_ids:
                    return StartupRecoverySummary((), ())
                task_rows = conn.execute(
                    """
                    SELECT task.id
                    FROM verification_tasks task
                    JOIN verifications verification
                      ON verification.id=task.verification_id
                    WHERE verification.status IN ('queued','running')
                      AND task.final_status=''
                    ORDER BY task.created_at ASC,task.id ASC
                    """
                ).fetchall()
                task_ids = tuple(
                    str(row["id"] or "") for row in task_rows
                    if str(row["id"] or "")
                )
                conn.execute(
                    """
                    UPDATE verification_tasks
                    SET final_status=?,result_json=?,finished_at=?
                    WHERE final_status=''
                      AND verification_id IN (
                          SELECT id FROM verifications
                          WHERE status IN ('queued','running')
                      )
                    """,
                    [
                        self.TASK_CANCELLED,
                        execution_result_json(cancelled_task_result(safe_reason)),
                        finished_at,
                    ],
                )
                conn.execute(
                    """
                    UPDATE verifications
                    SET status='failed',
                        fail_reason=CASE
                            WHEN fail_reason='' THEN ? ELSE fail_reason
                        END,
                        sanity_status=CASE
                            WHEN sanity_status IN ('pending','running') THEN 'skipped'
                            ELSE sanity_status
                        END,
                        finished_at=COALESCE(finished_at,?)
                    WHERE status IN ('queued','running')
                    """,
                    [safe_reason, finished_at],
                )
                return StartupRecoverySummary(verification_ids, task_ids)

            summary = self.db.write_transaction(_tx)
            self._runtime_by_task_id.clear()
            self._test_name_by_task_id.clear()
            return summary

    def verification_is_running(self, verification_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT status FROM verifications WHERE id=?",
            [verification_id],
        )
        return row is not None and str(row["status"] or "") == "running"

    def reset_runtime_state(self) -> None:
        """Forget all process-local indexes after exclusive artifact cleanup."""

        with self._runtime_lock.write_lock():
            self._runtime_by_task_id.clear()
            self._test_name_by_task_id.clear()
