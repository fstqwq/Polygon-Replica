#!/usr/bin/env python3
"""Offline replacement of verification_artifact_refs with the owner index."""

import argparse
import sqlite3
from pathlib import Path

from app.service.execution.codec import execution_result_from_json
from app.service.verification.artifact import task_artifact_rows


_CREATE_TABLE = """
CREATE TABLE verification_task_artifacts (
    verification_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    test_name TEXT NOT NULL,
    pass_number INTEGER NOT NULL CHECK(pass_number >= 0),
    role TEXT NOT NULL CHECK(role IN (
        'generated-input','accepted-answer','pass-input','pass-output',
        'pass-transcript','pass-stderr','pass-system','pass-feedback',
        'pass-team-feedback','pass-metadata','pass-compare-metadata'
    )),
    artifact_ref TEXT NOT NULL,
    download_filename TEXT NOT NULL,
    PRIMARY KEY(verification_id, task_id, pass_number, role),
    FOREIGN KEY(verification_id,task_id)
        REFERENCES verification_tasks(verification_id,id)
)
"""

_CREATE_TASK_IDENTITY_INDEX = """
CREATE UNIQUE INDEX idx_verification_tasks_verification_identity
ON verification_tasks(verification_id,id)
"""

_CREATE_INDEXES = (
    """CREATE INDEX idx_verification_task_artifacts_ref
       ON verification_task_artifacts(verification_id, artifact_ref)""",
    """CREATE INDEX idx_verification_task_artifacts_locator
       ON verification_task_artifacts(
           verification_id, task_id, test_name, pass_number, role
       )""",
)


def _object_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?",
        [name],
    ).fetchone()
    return row is not None


def _completion_ref(
    connection: sqlite3.Connection,
    *,
    verification_id: str,
    test_name: str,
    task_kind: str,
    artifact_ref: str,
) -> tuple[str, str]:
    if not artifact_ref:
        return "", ""
    rows = connection.execute(
        """
        SELECT id,result_json
        FROM verification_tasks
        WHERE verification_id=? AND test_name=? AND task_kind=?
          AND final_status<>''
        ORDER BY id
        """,
        [verification_id, test_name, task_kind],
    ).fetchall()
    matches: list[str] = []
    for row in rows:
        result = execution_result_from_json(str(row["result_json"]))
        if result.output_run_ref == artifact_ref:
            matches.append(str(row["id"]))
    if len(matches) != 1:
        raise RuntimeError(
            "cannot assign legacy artifact ref to exactly one task: "
            f"{verification_id} / {test_name} / {task_kind}"
        )
    return matches[0], artifact_ref


def upgrade(connection: sqlite3.Connection) -> dict[str, int]:
    """Apply and verify the ownership-table replacement atomically."""

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _object_exists(connection, "verification_task_artifacts"):
            raise RuntimeError("verification_task_artifacts already exists")
        if not _object_exists(connection, "verification_artifact_refs"):
            raise RuntimeError("verification_artifact_refs is missing")
        connection.execute(_CREATE_TASK_IDENTITY_INDEX)
        connection.execute(_CREATE_TABLE)
        for statement in _CREATE_INDEXES:
            connection.execute(statement)

        completion_refs: dict[str, tuple[str, str]] = {}
        legacy_rows = connection.execute(
            """
            SELECT verification_id,test_name,input_ref,answer_ref
            FROM verification_artifact_refs
            ORDER BY verification_id,test_name
            """
        ).fetchall()
        for legacy in legacy_rows:
            verification_id = str(legacy["verification_id"])
            test_name = str(legacy["test_name"])
            for key, task_kind in (
                ("input_ref", "generate-input"),
                ("answer_ref", "main-correct"),
            ):
                task_id, artifact_ref = _completion_ref(
                    connection,
                    verification_id=verification_id,
                    test_name=test_name,
                    task_kind=task_kind,
                    artifact_ref=str(legacy[key]),
                )
                if artifact_ref:
                    previous = completion_refs.get(task_id, ("", ""))
                    completion_refs[task_id] = (
                        artifact_ref if key == "input_ref" else previous[0],
                        artifact_ref if key == "answer_ref" else previous[1],
                    )

        task_count = 0
        ownership_rows = 0
        for task in connection.execute(
            """
            SELECT id,verification_id,test_name,final_status,result_json
            FROM verification_tasks
            ORDER BY verification_id,id
            """
        ).fetchall():
            task_id = str(task["id"])
            if not str(task["final_status"]):
                task_count += 1
                continue
            generated_input_ref, accepted_answer_ref = completion_refs.get(
                task_id,
                ("", ""),
            )
            rows = task_artifact_rows(
                verification_id=str(task["verification_id"]),
                task_id=task_id,
                test_name=str(task["test_name"]),
                result=execution_result_from_json(str(task["result_json"])),
                generated_input_ref=generated_input_ref,
                accepted_answer_ref=accepted_answer_ref,
            )
            connection.executemany(
                """
                INSERT INTO verification_task_artifacts(
                    verification_id,task_id,test_name,pass_number,role,
                    artifact_ref,download_filename
                ) VALUES(?,?,?,?,?,?,?)
                """,
                rows,
            )
            task_count += 1
            ownership_rows += len(rows)

        expected_completion_refs = sum(
            bool(str(row["input_ref"])) + bool(str(row["answer_ref"]))
            for row in legacy_rows
        )
        actual_completion_refs = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM verification_task_artifacts
                WHERE role IN ('generated-input','accepted-answer')
                """
            ).fetchone()[0]
        )
        if actual_completion_refs != expected_completion_refs:
            raise RuntimeError("legacy completion artifact backfill is incomplete")
        actual_ownership_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM verification_task_artifacts"
            ).fetchone()[0]
        )
        if actual_ownership_rows != ownership_rows:
            raise RuntimeError("verification artifact ownership backfill is incomplete")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"foreign key check failed: {foreign_keys!r}")

        connection.execute("DROP TABLE verification_artifact_refs")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity!r}")
        connection.execute("COMMIT")
        return {
            "tasks": task_count,
            "artifact_rows": ownership_rows,
            "legacy_completion_refs": expected_completion_refs,
        }
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index verification artifact ownership in a stopped deployment",
    )
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    database = args.db.absolute()
    if database.is_symlink() or not database.is_file():
        raise RuntimeError(f"SQLite database is unavailable: {database}")
    with sqlite3.connect(database, isolation_level=None) as connection:
        summary = upgrade(connection)
    print(
        "verification artifact ownership indexed: "
        f"tasks={summary['tasks']} artifacts={summary['artifact_rows']} "
        f"legacy_refs={summary['legacy_completion_refs']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
