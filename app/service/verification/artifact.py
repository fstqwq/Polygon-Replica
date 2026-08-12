"""Verification-owned artifact indexing and download lookup."""

import base64
import binascii
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.db import DB
from app.service.execution.model import ExecutionResult
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore


_PASS_ROLES = (
    ("input_ref", "pass-input", "input"),
    ("output_ref", "pass-output", "output"),
    ("transcript_ref", "pass-transcript", "transcript.txt"),
    ("stderr_ref", "pass-stderr", "program.err"),
    ("system_ref", "pass-system", "system.out"),
    ("judge_message_ref", "pass-feedback", "feedback.txt"),
    ("team_message_ref", "pass-team-feedback", "team-feedback.txt"),
    ("metadata_ref", "pass-metadata", "program.meta"),
    ("compare_metadata_ref", "pass-compare-metadata", "compare.meta"),
)


@dataclass(frozen=True)
class VerificationArtifact:
    payload: PayloadFile
    filename: str


def artifact_virtual_path(artifact_ref: str) -> str:
    """Encode an opaque runtime ref for the existing artifact route."""

    if not artifact_ref:
        raise ValueError("artifact ref is required")
    encoded = base64.urlsafe_b64encode(artifact_ref.encode("utf-8")).decode("ascii")
    return f"blob/{encoded.rstrip('=')}"


def _test_stem(test_name: str) -> str:
    return Path(test_name).stem or "test"


def _pass_filename(test_name: str, pass_number: int, kind: str) -> str:
    stem = _test_stem(test_name)
    if kind == "input":
        return test_name
    if kind == "output":
        return f"{stem}.out"
    return kind


def index_task_artifacts(
    connection: sqlite3.Connection,
    *,
    verification_id: str,
    task_id: str,
    test_name: str,
    result: ExecutionResult,
    generated_input_ref: str,
    accepted_answer_ref: str,
) -> None:
    """Replace one task's ownership rows inside its completion transaction."""

    rows = task_artifact_rows(
        verification_id=verification_id,
        task_id=task_id,
        test_name=test_name,
        result=result,
        generated_input_ref=generated_input_ref,
        accepted_answer_ref=accepted_answer_ref,
    )
    connection.execute(
        "DELETE FROM verification_task_artifacts WHERE task_id=?",
        [task_id],
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


def task_artifact_rows(
    *,
    verification_id: str,
    task_id: str,
    test_name: str,
    result: ExecutionResult,
    generated_input_ref: str,
    accepted_answer_ref: str,
) -> list[tuple[object, ...]]:
    """Return the canonical ownership rows for one completed task."""

    rows: list[tuple[object, ...]] = []
    if generated_input_ref:
        rows.append(
            (
                verification_id,
                task_id,
                test_name,
                0,
                "generated-input",
                generated_input_ref,
                test_name,
            )
        )
    if accepted_answer_ref:
        rows.append(
            (
                verification_id,
                task_id,
                test_name,
                0,
                "accepted-answer",
                accepted_answer_ref,
                f"{_test_stem(test_name)}.ans",
            )
        )
    for pass_result in result.passes:
        artifacts = pass_result.artifacts
        for attribute, role, filename_kind in _PASS_ROLES:
            artifact_ref = getattr(artifacts, attribute)
            if not artifact_ref:
                continue
            rows.append(
                (
                    verification_id,
                    task_id,
                    test_name,
                    pass_result.number,
                    role,
                    artifact_ref,
                    _pass_filename(test_name, pass_result.number, filename_kind),
                )
            )
    return rows


class VerificationArtifactQuery:
    """Authorize indexed refs and resolve available runtime payloads."""

    def __init__(self, database: DB, blob_store: RuntimeBlobStore) -> None:
        self._database = database
        self._blob_store = blob_store

    def _row_for_virtual_path(
        self,
        verification_id: str,
        virtual_path: str,
    ) -> tuple[str, str] | None:
        if not virtual_path or virtual_path.startswith("/") or "\\" in virtual_path:
            return None
        parts = PurePosixPath(virtual_path).parts
        if "/".join(parts) != virtual_path or any(part in {".", ".."} for part in parts):
            return None
        if len(parts) == 2 and parts[0] == "blob":
            encoded = parts[1]
            padding = "=" * ((4 - (len(encoded) % 4)) % 4)
            try:
                artifact_ref = base64.urlsafe_b64decode(
                    (encoded + padding).encode("ascii")
                ).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return None
            if artifact_virtual_path(artifact_ref) != virtual_path:
                return None
            row = self._database.fetch_one(
                """
                SELECT artifact_ref,download_filename
                FROM verification_task_artifacts
                WHERE verification_id=? AND artifact_ref=?
                ORDER BY task_id,pass_number,role
                LIMIT 1
                """,
                [verification_id, artifact_ref],
            )
            if row is None:
                return None
            return str(row["artifact_ref"]), str(row["download_filename"])
        if len(parts) == 3 and parts[0] == "output":
            row = self._database.fetch_one(
                """
                SELECT artifact_ref,download_filename
                FROM verification_task_artifacts
                WHERE verification_id=? AND task_id=? AND role IN (
                    'pass-output','pass-transcript'
                )
                ORDER BY pass_number DESC,role
                LIMIT 1
                """,
                [verification_id, parts[1]],
            )
            if row is None or str(row["download_filename"]) != parts[2]:
                return None
            return str(row["artifact_ref"]), str(row["download_filename"])
        if len(parts) == 2 and parts[0] == "tests":
            row = self._database.fetch_one(
                """
                SELECT artifact_ref,download_filename
                FROM verification_task_artifacts
                WHERE verification_id=? AND test_name=? AND role='generated-input'
                ORDER BY task_id
                LIMIT 1
                """,
                [verification_id, parts[1]],
            )
            if row is None or str(row["download_filename"]) != parts[1]:
                return None
            return str(row["artifact_ref"]), str(row["download_filename"])
        if len(parts) == 2 and parts[0] == "ans":
            test_name = f"{Path(parts[1]).stem}.in"
            row = self._database.fetch_one(
                """
                SELECT artifact_ref,download_filename
                FROM verification_task_artifacts
                WHERE verification_id=? AND test_name=? AND role='accepted-answer'
                ORDER BY task_id
                LIMIT 1
                """,
                [verification_id, test_name],
            )
            if row is None or str(row["download_filename"]) != parts[1]:
                return None
            return str(row["artifact_ref"]), str(row["download_filename"])
        return None

    def resolve(
        self,
        verification_id: str,
        virtual_path: str,
    ) -> VerificationArtifact | None:
        locator = self._row_for_virtual_path(verification_id, virtual_path)
        if locator is None:
            return None
        artifact_ref, filename = locator
        payload = self._blob_store.descriptor(artifact_ref)
        if payload is None:
            return None
        return VerificationArtifact(
            payload=payload,
            filename=filename,
        )
