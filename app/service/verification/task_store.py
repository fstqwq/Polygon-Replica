import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from threading import Condition, RLock
from typing import TypeVar, TypedDict, cast

from app.db import DB, now_iso
from app.service.execution.codec import (
    execution_result_from_json,
    execution_result_json,
)
from app.service.execution.model import ExecutionPassResult, ExecutionResult
from app.service.execution.policy import (
    execution_result_with_outcome,
    normalize_execution_result,
)
from app.service.platform.error_text import aux_display_text_limit_bytes, bounded_display_text
from app.service.verification.diagnostic import (
    DiagnosticMergeOutcome,
    TaskDiagnosticSnapshot,
    compose_task_diagnostic_display,
    merge_task_diagnostic_snapshot,
    new_task_diagnostic_item,
    task_diagnostic_snapshot_from_json,
    task_diagnostic_snapshot_json,
)
from app.service.verification.artifact import index_task_artifacts
from app.service.verification.lifecycle import (
    ActivationCommit,
    ActivationOutcome,
    ActivationPlan,
    ParentTransition,
    SanityFinish,
    StartupRecoverySummary,
    VerificationTransitionCommit,
    cancelled_task_result,
)
from app.service.verification.result_match import (
    verification_program_results_match,
)
from app.service.verification.task_completion import (
    CompletionCommit,
    TaskCompletion,
)
from app.service.verification.task_metadata import canonical_diagnostics
from app.service.verification.types import VerificationStatus, VerificationTaskStatus


class VerificationTaskContext(TypedDict):
    """Immutable task metadata and the identities of its current execution."""

    id: str
    verification_id: str
    task_kind: str
    source_path: str
    program_id: str
    test_name: str
    expected_behavior: str
    run_id: str
    judgehost_task_id: str


class VerificationTaskRow(VerificationTaskContext):
    predecessor_task_id: str
    queue_index: int
    status: VerificationTaskStatus
    result: ExecutionResult
    result_json: str
    verdict: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    answer_correct: bool
    compile_log: str
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
    status: VerificationTaskStatus
    verdict: str


class VerificationTaskReadRow(TypedDict):
    id: str
    task_kind: str
    source_path: str
    program_id: str
    test_name: str
    status: VerificationTaskStatus


@dataclass
class _VerificationCoordination:
    lock: RLock = field(default_factory=RLock)
    users: int = 0


@dataclass(frozen=True)
class _RuntimeTaskState:
    status: VerificationTaskStatus
    run_id: str
    judgehost_task_id: str
    started_at: str
    context: VerificationTaskContext
    result_json: str = ""
    result: ExecutionResult | None = None


def _stored_result(runtime: _RuntimeTaskState | None, text: str) -> ExecutionResult:
    if runtime is not None and runtime.result is not None and runtime.result_json == text:
        return runtime.result
    return execution_result_from_json(text)


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
    bounded_passes: list[ExecutionPassResult] = []
    for pass_result in result.passes:
        feedback = bounded_display_text(pass_result.feedback, limit_bytes=limit_bytes)
        bounded_passes.append(
            pass_result if feedback == pass_result.feedback
            else replace(pass_result, feedback=feedback)
        )
    passes = tuple(bounded_passes)
    diagnostics = canonical_diagnostics(
        list(result.compile.diagnostics),
        list_limit=64,
        message_limit=limit_bytes,
    )["rows"]
    error = bounded_display_text(result.outcome.error, limit_bytes=limit_bytes)
    feedback = bounded_display_text(result.outcome.feedback, limit_bytes=limit_bytes)
    compile_log = bounded_display_text(result.compile.log, limit_bytes=limit_bytes)
    warnings = tuple(
        bounded_display_text(warning.message, limit_bytes=limit_bytes)
        for warning in result.warnings
    )
    if (
        passes == result.passes
        and diagnostics == list(result.compile.diagnostics)
        and error == result.outcome.error
        and feedback == result.outcome.feedback
        and compile_log == result.compile.log
        and warnings == tuple(warning.message for warning in result.warnings)
    ):
        return result
    return normalize_execution_result(
        passes=passes,
        verdict=result.verdict,
        score_text=result.score_text,
        answer_correct=result.answer_correct,
        error=error,
        feedback=feedback,
        compile_log=compile_log,
        compile_diagnostics=diagnostics,
        warnings=warnings,
    )


class VerificationTaskStore:

    def __init__(self, db: DB) -> None:
        self.db = db
        # Only special paths coordinate across SQL and memory publication.
        # Never wait for SQLite or coordination while holding the runtime lock.
        self._coordination: dict[str, _VerificationCoordination] = {}
        self._runtime_lock = RLock()
        self._admission_condition = Condition(self._runtime_lock)
        self._paused_verifications: set[str] = set()
        self._runtime_by_task_id: dict[str, _RuntimeTaskState] = {}
        self._admissible_tasks: dict[str, VerificationTaskContext] = {}
        self._input_owners: dict[str, dict[str, tuple[str, str]]] = {}

    @contextmanager
    def _coordinate(self, verification_id: str) -> Iterator[None]:
        with self._runtime_lock:
            scope = self._coordination.get(verification_id)
            if scope is None:
                scope = _VerificationCoordination()
                self._coordination[verification_id] = scope
            scope.users += 1
        try:
            with scope.lock:
                yield
        finally:
            with self._runtime_lock:
                scope.users -= 1
                if not scope.users:
                    del self._coordination[verification_id]

    def _completion_coordination(
        self, completions: dict[str, TaskCompletion],
    ) -> str:
        # Task metadata never changes. Prefer already admitted metadata; replay
        # and rebuilt-store callers can recover it without a write transaction.
        metadata: dict[str, tuple[str, str]] = {}
        with self._runtime_lock:
            for task_id in completions:
                context = self._admissible_tasks.get(task_id)
                if context is None:
                    runtime = self._runtime_by_task_id.get(task_id)
                    context = None if runtime is None else runtime.context
                if context is not None:
                    metadata[task_id] = (context["verification_id"], context["task_kind"])
        missing = completions.keys() - metadata.keys()
        if missing:
            rows = self.db.fetch_all(
                "SELECT id,verification_id,task_kind FROM verification_tasks "
                f"WHERE id IN ({','.join('?' for _ in missing)})", list(missing),
            )
            for row in rows:
                metadata[str(row["id"])] = (str(row["verification_id"]), str(row["task_kind"]))
        if len(metadata) != len(completions):
            raise RuntimeError("unknown verification task completion")
        verification_ids = {item[0] for item in metadata.values()}
        if len(verification_ids) != 1:
            raise RuntimeError("task completion batch crosses verifications")
        for task_id, completion in completions.items():
            task_kind = metadata[task_id][1]
            if (
                task_kind == "generate-input"
                or completion.result.verdict.upper() == "SK"
                or completion.status == VerificationTaskStatus.CANCELLED
                or (completion.fail_reason and task_kind in _HARD_FAILURE_TASK_KINDS)
            ):
                return metadata[task_id][0]
        return ""

    def _limit_bytes(self) -> int:
        return aux_display_text_limit_bytes(self.db.config_values.snapshot())

    def _normalize_display_text(self, value: str) -> str:
        return bounded_display_text(value, limit_bytes=self._limit_bytes())

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
        deleted_verification_ids: set[str] = set()
        with ExitStack() as scopes:
            verifications = self.db.fetch_all(
                "SELECT id FROM verifications WHERE problem_id=? ORDER BY id", [problem_id],
            )
            coordinated_ids = {str(verification["id"]) for verification in verifications}
            for verification_id in sorted(coordinated_ids):
                scopes.enter_context(self._coordinate(verification_id))

            def _tx(conn: sqlite3.Connection) -> _SnapshotValue:
                nonlocal deleted_task_ids
                rows = conn.execute(
                    """
                    SELECT task.id,task.verification_id
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
                deleted_verification_ids.clear()
                deleted_verification_ids.update(str(row["verification_id"]) for row in rows)
                if deleted_verification_ids - coordinated_ids:
                    raise ValueError("cannot delete problem while verification history is changing")
                with self._runtime_lock:
                    has_runtime = any(
                        task_id in self._runtime_by_task_id
                        for task_id in deleted_task_ids
                    )
                if has_runtime:
                    raise ValueError(
                        "cannot delete problem while verification runtime is draining"
                    )
                return delete_metadata(conn)

            result = self.db.write_transaction(_tx)
            with self._runtime_lock:
                for task_id in deleted_task_ids:
                    self._admissible_tasks.pop(task_id, None)
            for verification_id in deleted_verification_ids:
                self._input_owners.pop(verification_id, None)
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
        detail = plan.detail
        now_text = now_iso()

        with self._coordinate(plan.verification_id):
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
                        outcome: ActivationOutcome = "missing"
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
            with self._runtime_lock:
                self._admissible_tasks.update({
                    task.task_id: VerificationTaskContext(
                        id=task.task_id, verification_id=plan.verification_id,
                        program_id=task.program_id, test_name=task.test_name,
                        task_kind=task.task_kind, source_path=task.source_path,
                        expected_behavior=task.expected_behavior,
                        run_id="", judgehost_task_id="",
                    )
                    for task in ordered_tasks
                })
            return commit

    def _runtime_status(self, row: dict[str, object]) -> _RuntimeTaskState | None:
        with self._runtime_lock:
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
        with self._runtime_lock:
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
            status = VerificationTaskStatus(final_status)
        elif runtime is not None:
            status = runtime.status
        else:
            status = VerificationTaskStatus.PENDING
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
        result = _stored_result(runtime, result_json)
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
            status = VerificationTaskStatus(final_status)
        elif runtime is not None:
            status = runtime.status
        else:
            status = VerificationTaskStatus.PENDING
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
                   COALESCE((
                       SELECT artifact.artifact_ref
                       FROM verification_task_artifacts artifact
                       WHERE artifact.verification_id=task.verification_id
                         AND artifact.test_name=task.test_name
                         AND artifact.role='generated-input'
                       ORDER BY artifact.task_id
                       LIMIT 1
                   ),'') AS input_ref,
                   COALESCE((
                       SELECT artifact.artifact_ref
                       FROM verification_task_artifacts artifact
                       WHERE artifact.verification_id=task.verification_id
                         AND artifact.test_name=task.test_name
                         AND artifact.role='accepted-answer'
                       ORDER BY artifact.task_id
                       LIMIT 1
                   ),'') AS answer_ref,
                   COALESCE(diagnostic.snapshot_json,'') AS late_diagnostic_json
            FROM verification_tasks task
            LEFT JOIN verification_task_diagnostics diagnostic
              ON diagnostic.task_id=task.id
            WHERE task.verification_id=?
            """,
            [verification_id],
        ).fetchall()
        with self._runtime_lock:
            runtimes = dict(self._runtime_by_task_id)
        ordered = sorted((dict(row) for row in rows), key=self._row_order)
        values: list[dict[str, object]] = []
        limit_bytes = self._limit_bytes()
        for index, row in enumerate(ordered, start=1):
            task_id = str(row["id"] or "")
            decorated = dict(
                self._decorate_row_with_runtime(
                    index,
                    row,
                    runtime=runtimes.get(task_id),
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
                limit_bytes=limit_bytes,
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
        """Read one SQLite snapshot; runtime overlays use short memory locks."""

        with self.db.conn() as conn:
            conn.execute("BEGIN")
            return reader(conn)

    def runtime_row(self, task_id: str) -> VerificationTaskRow | None:
        if not task_id:
            return None
        with self._runtime_lock:
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

    def bound_task_context(self, task_id: str) -> VerificationTaskContext | None:
        """Snapshot a validated binding; durable terminal state stays in SQLite."""

        with self._runtime_lock:
            runtime = self._runtime_by_task_id.get(task_id)
        return None if runtime is None else runtime.context.copy()

    def _wait_for_admission_locked(self, verification_id: str) -> None:
        # Bulk terminal decisions pause admission until commit or rollback.
        while verification_id in self._paused_verifications:
            self._admission_condition.wait()

    def bind_and_expose_judgehost_runtime(
        self,
        verification_task_id: str,
        *,
        expected_verification_id: str,
        expected_program_id: str,
        expected_test_name: str,
        run_id: str,
        judgehost_task_id: str,
        expose: Callable[[], None],
    ) -> bool:
        if not verification_task_id or not run_id or not judgehost_task_id:
            raise ValueError("verification Judgehost binding identities are required")
        with self._admission_condition:
            self._wait_for_admission_locked(expected_verification_id)
            context = self._admissible_tasks.get(verification_task_id)
            if (
                context is None
                or context["verification_id"] != expected_verification_id
                or context["program_id"] != expected_program_id
                or context["test_name"] != expected_test_name
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
            bound_context = context.copy()
            bound_context["run_id"] = run_id
            bound_context["judgehost_task_id"] = judgehost_task_id
            runtime = _RuntimeTaskState(
                status=VerificationTaskStatus.QUEUED,
                run_id=run_id,
                judgehost_task_id=judgehost_task_id,
                started_at="",
                context=bound_context,
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
        with self._runtime_lock:
            current = self._runtime_by_task_id.get(verification_task_id)
            if current is None or current.judgehost_task_id != judgehost_task_id:
                return False
            self._runtime_by_task_id.pop(verification_task_id, None)
            return True

    def set_task_leased(self, task_id: str) -> bool:
        with self._admission_condition:
            current = self._runtime_by_task_id.get(task_id)
            if current is None:
                return False
            self._wait_for_admission_locked(current.context["verification_id"])
            current = self._runtime_by_task_id.get(task_id)
            if current is None or task_id not in self._admissible_tasks:
                return False
            self._runtime_by_task_id[task_id] = replace(
                current,
                status=VerificationTaskStatus.LEASED,
                started_at=current.started_at or now_iso(),
            )
            return True

    def requeue_leased_tasks(
        self,
        verification_id: str,
        judgehost_task_ids: list[str],
    ) -> list[str]:
        allowed = set(judgehost_task_ids)
        if not allowed:
            return []
        changed: list[str] = []
        with self._admission_condition:
            self._wait_for_admission_locked(verification_id)
            for task_id, runtime in self._runtime_by_task_id.items():
                if (
                    runtime.status == VerificationTaskStatus.LEASED
                    and runtime.judgehost_task_id in allowed
                    and runtime.context["verification_id"] == verification_id
                    and task_id in self._admissible_tasks
                ):
                    self._runtime_by_task_id[task_id] = replace(
                        runtime, status=VerificationTaskStatus.QUEUED, started_at="",
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
                        VerificationTaskStatus.DONE.value,
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
                    final_status_by_id[child_id] = VerificationTaskStatus.DONE.value
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
                VerificationTaskStatus.CANCELLED.value,
                execution_result_json(cancelled_task_result(reason)),
                finished_at,
                verification_id,
            ],
        )
        return task_ids

    def _completed_solution_program_failure(
        self,
        conn: sqlite3.Connection,
        *,
        verification_id: str,
        program_ids: tuple[str, ...],
    ) -> str:
        if not program_ids:
            return ""
        completed_program_ids = tuple(
            program_id
            for program_id in program_ids
            if conn.execute(
                """
                SELECT 1 FROM verification_tasks
                WHERE verification_id=? AND final_status=''
                  AND task_kind='solution-run' AND program_id=?
                LIMIT 1
                """,
                [verification_id, program_id],
            ).fetchone() is None
        )
        if not completed_program_ids:
            return ""
        rows = conn.execute(
            f"""
            SELECT id,program_id,source_path,test_name,expected_behavior,
                   final_status,result_json
            FROM verification_tasks
            WHERE verification_id=? AND task_kind='solution-run'
              AND program_id IN ({','.join('?' for _ in completed_program_ids)})
            """,
            [verification_id, *completed_program_ids],
        ).fetchall()
        rows_by_program: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            task_row = dict(row)
            rows_by_program.setdefault(
                str(task_row["program_id"] or ""), []
            ).append(task_row)

        for program_id in completed_program_ids:
            program_rows = rows_by_program.get(program_id)
            if not program_rows:
                raise RuntimeError(
                    f"verification solution program {program_id} has no tasks"
                )
            final_statuses = {
                str(row["final_status"] or "") for row in program_rows
            }
            if "" in final_statuses:
                continue
            if final_statuses != {VerificationTaskStatus.DONE.value}:
                continue
            source_paths = {str(row["source_path"] or "") for row in program_rows}
            expected_behaviors = {
                str(row["expected_behavior"] or "") for row in program_rows
            }
            if len(source_paths) != 1 or len(expected_behaviors) != 1:
                raise RuntimeError(
                    f"verification solution program {program_id} is inconsistent"
                )
            ordered_rows = sorted(
                program_rows,
                key=lambda row: (
                    _test_name_order(str(row["test_name"] or "")),
                    str(row["id"] or ""),
                ),
            )
            matched, reason = verification_program_results_match(
                next(iter(expected_behaviors)),
                (
                    execution_result_from_json(str(row["result_json"] or "{}"))
                    for row in ordered_rows
                ),
            )
            if not matched:
                source_path = next(iter(source_paths))
                origin = " / ".join(
                    token for token in ("solution-run", source_path) if token
                )
                return self._normalize_display_text(
                    f"{origin}: {reason or 'verification mismatch'}"
                )
        return ""

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
        terminal_statuses = {
            VerificationTaskStatus.DONE,
            VerificationTaskStatus.FAILED,
            VerificationTaskStatus.CANCELLED,
        }
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
            completion.status in {VerificationTaskStatus.FAILED, VerificationTaskStatus.CANCELLED}
            and not completion.fail_reason
            for completion in normalized_by_id.values()
        ):
            raise ValueError("failed or cancelled task completion needs a reason")

        coordinated_id = self._completion_coordination(normalized_by_id)
        scope = self._coordinate(coordinated_id) if coordinated_id else nullcontext()
        with scope:
            input_owners: dict[str, tuple[str, str]] | None = None
            new_input_owners: dict[str, tuple[str, str]] = {}
            stored_results: dict[str, tuple[str, ExecutionResult]] = {}
            paused_verification_id = ""

            def _tx(conn: sqlite3.Connection) -> CompletionCommit:
                nonlocal input_owners, paused_verification_id
                # Each transaction retry starts with only committed state.
                input_owners = None
                new_input_owners.clear()
                stored_results.clear()
                # Only a replay needs the previously committed artifact refs.
                rows = conn.execute(
                    f"""
                    SELECT t.id,t.verification_id,t.task_kind,t.program_id,
                           t.test_name,
                           t.final_status,t.result_json,
                           COALESCE((
                               SELECT artifact.artifact_ref
                               FROM verification_task_artifacts artifact
                               WHERE t.final_status<>''
                                 AND artifact.verification_id=t.verification_id
                                 AND artifact.test_name=t.test_name
                                 AND artifact.role='generated-input'
                               ORDER BY artifact.task_id
                               LIMIT 1
                           ),'') AS input_ref,
                           COALESCE((
                               SELECT artifact.artifact_ref
                               FROM verification_task_artifacts artifact
                               WHERE t.final_status<>''
                                 AND artifact.verification_id=t.verification_id
                                 AND artifact.test_name=t.test_name
                                 AND artifact.role='accepted-answer'
                               ORDER BY artifact.task_id
                               LIMIT 1
                           ),'') AS answer_ref
                    FROM verification_tasks t
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
                if any(
                    not str(row["final_status"] or "")
                    and str(row["task_kind"] or "") == "generate-input"
                    for row in rows
                ):
                    input_owners = self._input_owners.get(verification_id)
                    if input_owners is None:
                        input_owners = {}
                        owner_rows = conn.execute(
                            """
                            SELECT id,test_name,result_json
                            FROM verification_tasks
                            WHERE verification_id=? AND task_kind='generate-input'
                              AND final_status=?
                            ORDER BY finished_at ASC,id ASC
                            """,
                            [verification_id, VerificationTaskStatus.DONE.value],
                        ).fetchall()
                        for owner_row in owner_rows:
                            owner_result = execution_result_from_json(
                                str(owner_row["result_json"] or "{}")
                            )
                            output_ref = owner_result.output_run_ref
                            if output_ref and owner_result.verdict.upper() != "SK":
                                input_owners.setdefault(
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
                affected_solution_program_ids: list[str] = []
                affected_solution_program_id_set: set[str] = set()
                new_failure_reason = ""
                hard_failure_reason = ""
                for task_id in task_ids:
                    incoming = normalized_by_id[task_id]
                    row = rows_by_id[task_id]
                    current_status = str(row["final_status"] or "")
                    if current_status:
                        with self._runtime_lock:
                            runtime = self._runtime_by_task_id.get(task_id)
                        current_result = _stored_result(
                            runtime, str(row["result_json"] or "{}")
                        )
                        already_terminal_task_ids.add(task_id)
                        effective.append(
                            TaskCompletion(
                                task_id=task_id,
                                status=VerificationTaskStatus(current_status),
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
                            current_status == VerificationTaskStatus.DONE.value
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
                        and incoming.status == VerificationTaskStatus.DONE
                        and result.verdict.upper() != "SK"
                        and output_ref
                    ):
                        assert input_owners is not None
                        owner = new_input_owners.get(output_ref) or input_owners.get(output_ref)
                        if owner is None:
                            new_input_owners[output_ref] = (
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
                    result_json = execution_result_json(result)
                    stored_results[task_id] = (result_json, result)
                    conn.execute(
                        """
                        UPDATE verification_tasks
                        SET final_status=?,result_json=?,finished_at=?
                        WHERE id=? AND final_status=''
                        """,
                        [
                            effective_completion.status.value,
                            result_json,
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
                    if task_kind == "solution-run":
                        program_id = str(row["program_id"] or "")
                        if program_id not in affected_solution_program_id_set:
                            affected_solution_program_id_set.add(program_id)
                            affected_solution_program_ids.append(program_id)
                    if effective_completion.fail_reason and not new_failure_reason:
                        new_failure_reason = effective_completion.fail_reason
                    if (
                        effective_completion.fail_reason
                        and not hard_failure_reason
                        and (
                            effective_completion.status == VerificationTaskStatus.CANCELLED
                            or task_kind in _HARD_FAILURE_TASK_KINDS
                        )
                    ):
                        hard_failure_reason = effective_completion.fail_reason

                    index_task_artifacts(
                        conn,
                        verification_id=verification_id,
                        task_id=task_id,
                        test_name=str(row["test_name"] or ""),
                        result=effective_completion.result,
                        generated_input_ref=effective_completion.input_ref,
                        accepted_answer_ref=effective_completion.answer_ref,
                    )
                    if (
                        task_kind == "generate-input"
                        and effective_completion.status == VerificationTaskStatus.DONE
                        and effective_completion.result.verdict.upper() == "SK"
                    ):
                        skipped_task_ids.add(task_id)

                # Only generators own dependency-subtree skipping. A replay of
                # a skipped descendant returns its durable result without making
                # another admission decision for that subtree.
                skipped_generator_ids = {
                    task_id for task_id in skipped_task_ids
                    if str(rows_by_id[task_id]["task_kind"]) == "generate-input"
                }
                if skipped_generator_ids:
                    with self._runtime_lock:
                        self._paused_verifications.add(verification_id)
                        paused_verification_id = verification_id
                        active_task_ids = set(self._runtime_by_task_id)
                    skipped_task_ids.update(
                        self._skip_pending_descendants(
                            conn,
                            verification_id=verification_id,
                            root_task_ids=skipped_generator_ids,
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
                            VerificationTaskStatus.DONE.value,
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
                if skipped_task_ids and not new_failure_reason:
                    skipped_rows = conn.execute(
                        f"""
                        SELECT DISTINCT program_id
                        FROM verification_tasks
                        WHERE task_kind='solution-run'
                          AND id IN ({','.join('?' for _ in skipped_task_ids)})
                        ORDER BY program_id
                        """,
                        sorted(skipped_task_ids),
                    ).fetchall()
                    for skipped_row in skipped_rows:
                        program_id = str(skipped_row["program_id"] or "")
                        if program_id not in affected_solution_program_id_set:
                            affected_solution_program_id_set.add(program_id)
                            affected_solution_program_ids.append(program_id)
                if not new_failure_reason:
                    new_failure_reason = self._completed_solution_program_failure(
                        conn,
                        verification_id=verification_id,
                        program_ids=tuple(affected_solution_program_ids),
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
                    with self._runtime_lock:
                        self._paused_verifications.add(verification_id)
                        paused_verification_id = verification_id
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

            try:
                committed = self.db.write_transaction(_tx)
                # Publish only durable owners/results. A failed or retried transaction
                # must never introduce a duplicate-input owner into the runtime map.
                if input_owners is not None:
                    input_owners.update(new_input_owners)
                    self._input_owners[committed.verification_id] = input_owners
                if committed.parent_transition:
                    # The last ordinary completion can overtake a generator's
                    # post-commit publication. Drain that publication before cleanup.
                    with self._coordinate(committed.verification_id):
                        self._input_owners.pop(committed.verification_id, None)
                with self._runtime_lock:
                    for task_id in (
                        committed.committed_task_ids | committed.already_terminal_task_ids
                        | committed.skipped_task_ids | committed.cancelled_task_ids
                    ):
                        self._admissible_tasks.pop(task_id, None)
                    for task_id, (text, result) in stored_results.items():
                        runtime = self._runtime_by_task_id.get(task_id)
                        incoming = normalized_by_id[task_id]
                        if (
                            runtime is not None
                            and runtime.run_id == incoming.run_id
                            and runtime.judgehost_task_id == incoming.judgehost_task_id
                        ):
                            self._runtime_by_task_id[task_id] = replace(
                                runtime, result_json=text, result=result,
                            )
                return committed
            finally:
                if paused_verification_id:
                    with self._admission_condition:
                        self._paused_verifications.remove(paused_verification_id)
                        self._admission_condition.notify_all()

    def transition_verification_terminal(
        self,
        verification_id: str,
        *,
        status: VerificationStatus,
        reason: str,
    ) -> VerificationTransitionCommit:
        safe_reason = self._normalize_display_text(
            reason or f"verification {status.value}"
        )
        finished_at = now_iso()
        with self._coordinate(verification_id):
            with self._runtime_lock:
                self._paused_verifications.add(verification_id)

            def _tx(conn: sqlite3.Connection) -> VerificationTransitionCommit:
                row = conn.execute(
                    "SELECT status FROM verifications WHERE id=?",
                    [verification_id],
                ).fetchone()
                if row is None:
                    return VerificationTransitionCommit(
                        verification_id=verification_id,
                        outcome="missing",
                        status=None,
                    )
                current_status = str(row["status"] or "")
                if current_status not in {"queued", "running"}:
                    return VerificationTransitionCommit(
                        verification_id=verification_id,
                        outcome="closed",
                        status=VerificationStatus(current_status),
                    )
                cursor = conn.execute(
                    """
                    UPDATE verifications
                    SET status=?,
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
                    [status.value, safe_reason, finished_at, verification_id],
                )
                if int(cursor.rowcount or 0) != 1:
                    raise RuntimeError(
                        f"verification {verification_id} terminal transition was lost"
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
                    status=status,
                    cancelled_task_ids=frozenset(cancelled),
                )

            try:
                committed = self.db.write_transaction(_tx)
                with self._runtime_lock:
                    for task_id in committed.cancelled_task_ids:
                        self._admissible_tasks.pop(task_id, None)
                self._input_owners.pop(verification_id, None)
                return committed
            finally:
                with self._admission_condition:
                    self._paused_verifications.remove(verification_id)
                    self._admission_condition.notify_all()

    def finish_sanity(
        self,
        finish: SanityFinish,
        *,
        write_detail: Callable[
            [sqlite3.Connection, str, dict[str, object]],
            None,
        ],
    ) -> VerificationTransitionCommit:
        detail = finish.detail
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
                    status=VerificationStatus.OK,
                )
            row = conn.execute(
                "SELECT status FROM verifications WHERE id=?",
                [finish.verification_id],
            ).fetchone()
            if row is None:
                return VerificationTransitionCommit(
                    verification_id=finish.verification_id,
                    outcome="missing",
                    status=None,
                )
            return VerificationTransitionCommit(
                verification_id=finish.verification_id,
                outcome="closed",
                status=VerificationStatus(str(row["status"])),
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
        with self._runtime_lock:
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
            reason or "interrupted by application restart"
        )
        finished_at = now_iso()
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
                    VerificationTaskStatus.CANCELLED.value,
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
        with self._runtime_lock:
            self._runtime_by_task_id.clear()
            self._admissible_tasks.clear()
        self._input_owners.clear()
        return summary

    def verification_is_running(self, verification_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT status FROM verifications WHERE id=?",
            [verification_id],
        )
        return row is not None and str(row["status"] or "") == "running"

    def reset_runtime_state(self) -> None:
        """Forget all process-local indexes after exclusive artifact cleanup."""

        with self._runtime_lock:
            self._runtime_by_task_id.clear()
            self._admissible_tasks.clear()
        self._input_owners.clear()
