from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from app.config import ConfigValues
from app.db import DB
from app.service.disk.verification_store import VerificationStore
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.platform.fs.layout import FsManager
from app.service.problem.test_spec import (
    TestSpecEntry,
    parse_gen_command_tokens,
)
from app.service.problem.build_config import BuildConfig
from app.service.repository.workspace import WorkspaceService
from app.service.verification.types import (
    Kind,
    WorkspaceVerificationKey,
    WorkspaceVerificationRow,
)
from app.service.verification.identity import (
    canonical_verification_id,
    new_verification_id,
)
from app.service.verification.lifecycle import (
    ActivationCommit,
    ActivationPlan,
    AdmissionCommit,
    SanityFinish,
    StartupRecoverySummary,
    VerificationAdmission,
    VerificationSnapshot,
    VerificationSnapshotRecord,
    VerificationTransitionCommit,
)

from app.service.verification.read_model import (
    program_ids,
    running_tasks,
    solution_source_paths,
    task_counts,
)
from app.service.verification.signature import verification_manifest
from app.service.verification.source import select_source
from app.service.verification.task_store import VerificationTaskStore
from app.service.execution.codec import execution_result_from_json
from app.service.verification.test_spec import manual_test_sources, prepare_tests_spec_runtime

from app.service.judgehost.api import Judgehost


CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
GENERATOR_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
RUN_TEST_NAME_RE = re.compile(r"^[0-9]{3}\.in$")
_DETAIL_SCALAR_DEFAULTS: dict[str, object] = {
    "mode": "pass-fail",
    "pass_limit": 1,
    "run_config_json": "",
    "error": "",
    "failed_step": "",
    "failed_check": "",
    "failed_test": "",
    "sanity_status": "",
    "sanity_checked_count": 0,
    "validation_status": "",
    "validated_count": 0,
}

class VerificationService:
    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        judgehost_task_service: Judgehost,
        task_store: VerificationTaskStore,
        runtime_blob_store: RuntimeBlobStore,
        fs_manager: FsManager,
        config_values: ConfigValues,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.judgehost_task_service = judgehost_task_service
        self.task_store = task_store
        self.runtime_blob_store = runtime_blob_store
        self.fs_manager = fs_manager
        self._config_values = config_values
        self._verification_store = VerificationStore(db)

    def export_runtime_verification(self, problem_id: int, verification_id: str) -> dict[str, object] | None:
        row = self.verification_record(verification_id)
        if row is None or int(row["problem_id"]) != int(problem_id):
            return None
        return {
            "id": str(row["id"] or ""),
            "status": str(row["status"] or ""),
            "details": self.verification_detail(verification_id),
        }

    def has_export_detail_verification(self, problem_id: int, verification_id: str) -> bool:
        row = self._verification_store.get_status_row(int(problem_id), verification_id)
        if row is None:
            return False
        return row["status"] in {"queued", "running", "ok", "failed"}

    def artifact_path_for_problem_artifact(self, problem_id: int, artifact_id: str) -> str:
        if artifact_id.startswith("p-"):
            row = self.db.fetch_one(
                "SELECT id FROM previews WHERE id=? AND problem_id=?",
                [artifact_id, int(problem_id)],
            )
            return "" if row is None else str(self.fs_manager.resolve_preview_root(artifact_id))
        row = self.db.fetch_one(
            "SELECT id FROM verifications WHERE id=? AND problem_id=?",
            [artifact_id, int(problem_id)],
        )
        return "" if row is None else str(self.fs_manager.resolve_verification_root(artifact_id))

    def artifact_path_for_verification(self, verification_id: str) -> str:
        row = self.db.fetch_one("SELECT id FROM verifications WHERE id=?", [verification_id])
        return "" if row is None else str(self.fs_manager.resolve_verification_root(verification_id))

    def allocate_verification_id(self) -> str:
        return new_verification_id()

    def workspace_verification_id_for_run(self, problem_id: int, workspace_id: int, run_id: str) -> str:
        token = run_id
        if not token:
            return ""
        task_store = self.task_store
        for row in self._verification_store.list_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=512,
            kinds=(Kind.ALL, Kind.SAMPLE, Kind.CUSTOM),
        ):
            verification_id = str(row["id"])
            for task_row in task_store.list_rows(verification_id):
                if str(task_row["run_id"] or "") == token:
                    return verification_id
        return ""

    def workspace_verification_exists(self, problem_id: int, workspace_id: int, verification_id: str) -> bool:
        return self._verification_store.workspace_verification_exists(int(problem_id), int(workspace_id), verification_id)

    def workspace_artifact_exists(self, problem_id: int, workspace_id: int, artifact_id: str) -> bool:
        return self._verification_store.workspace_artifact_exists(int(problem_id), int(workspace_id), artifact_id)

    def workspace_verification_detail(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> dict[str, object] | None:
        snapshot = self.verification_snapshot(verification_id)
        if snapshot is None:
            return None
        record = snapshot["record"]
        if (
            int(record["problem_id"]) != int(problem_id)
            or record["workspace_id"] != int(workspace_id)
        ):
            return None
        return {
            "id": record["id"],
            "status": record["status"],
            "details": snapshot["detail"],
        }

    def latest_problem_verification_id_for_signature(self, problem_id: int, signature: str) -> str:
        return self._verification_store.latest_problem_verification_id_for_signature(int(problem_id), signature)

    def latest_workspace_verification_id_for_signature(
        self,
        problem_id: int,
        workspace_id: int,
        signature: str,
        *,
        ok_only: bool = False,
    ) -> str:
        return self._verification_store.latest_workspace_verification_id_for_signature(
            int(problem_id),
            int(workspace_id),
            signature,
            ok_only=bool(ok_only),
        )

    def verification_record(self, verification_id: str) -> dict[str, object] | None:
        row = self._verification_store.record_row(verification_id)
        if row is None:
            return None
        return dict(row)

    def _ordered_detail_tokens(
        self,
        verification_id: str,
        table_name: str,
        column_name: str,
        *,
        conn=None,
    ) -> list[str]:
        sql = f"""
            SELECT {column_name}
            FROM {table_name}
            WHERE verification_id=?
            ORDER BY ordinal ASC
            """
        params = [str(verification_id or "").strip()]
        rows = self.db.fetch_all(sql, params) if conn is None else conn.execute(sql, params).fetchall()
        values: list[str] = []
        for row in rows:
            token = str(row[column_name] or "")
            if token:
                values.append(token)
        return values

    def _verification_tests_meta_rows(
        self,
        verification_id: str,
        *,
        conn=None,
    ) -> list[dict[str, object]]:
        sql = """
            SELECT ordinal,test_name,source_kind,source_id,is_sample,sample_input_custom,sample_output_custom,
                   sample_output_validate,description,source_path,command_text,payload_source_path
            FROM verification_tests_meta
            WHERE verification_id=?
            ORDER BY ordinal ASC
            """
        params = [str(verification_id or "").strip()]
        rows = self.db.fetch_all(sql, params) if conn is None else conn.execute(sql, params).fetchall()
        values: list[dict[str, object]] = []
        for row in rows:
            item: dict[str, object] = {
                "index": max(1, int(row["ordinal"] or 0)),
                "test_name": str(row["test_name"] or ""),
                "kind": str(row["source_kind"] or ""),
                "id": str(row["source_id"] or ""),
                "sample": bool(int(row["is_sample"] or 0)),
                "sample_input_custom": bool(int(row["sample_input_custom"] or 0)),
                "sample_output_custom": bool(int(row["sample_output_custom"] or 0)),
                "sample_output_validate": bool(int(row["sample_output_validate"] or 0)),
                "desc": str(row["description"] or ""),
                "source": str(row["source_path"] or ""),
            }
            command_text = str(row["command_text"] or "")
            if command_text:
                item["command"] = command_text
            payload_source_path = str(row["payload_source_path"] or "")
            if payload_source_path:
                item["payload_source"] = payload_source_path
            values.append(item)
        return values

    def _verification_sanity_check_results(
        self,
        verification_id: str,
        *,
        conn=None,
    ) -> list[dict[str, object]]:
        safe_verification_id = str(verification_id or "").strip()
        check_sql = """
            SELECT ordinal,check_name,status,checked_count
            FROM verification_sanity_checks
            WHERE verification_id=?
            ORDER BY ordinal ASC
            """
        message_sql = """
            SELECT check_name,ordinal,severity,test_name,message
            FROM verification_sanity_check_messages
            WHERE verification_id=?
            ORDER BY check_name ASC, ordinal ASC
            """
        if conn is None:
            check_rows = self.db.fetch_all(check_sql, [safe_verification_id])
            message_rows = self.db.fetch_all(message_sql, [safe_verification_id])
        else:
            check_rows = conn.execute(check_sql, [safe_verification_id]).fetchall()
            message_rows = conn.execute(message_sql, [safe_verification_id]).fetchall()
        messages_by_check: dict[str, list[dict[str, object]]] = {}
        for row in message_rows:
            check_name = str(row["check_name"] or "")
            if not check_name:
                continue
            messages_by_check.setdefault(check_name, []).append(
                {
                    "severity": str(row["severity"] or ""),
                    "test_name": str(row["test_name"] or ""),
                    "message": str(row["message"] or ""),
                }
            )
        results: list[dict[str, object]] = []
        for row in check_rows:
            check_name = str(row["check_name"] or "")
            if not check_name:
                continue
            results.append(
                {
                    "name": check_name,
                    "status": str(row["status"] or ""),
                    "checked_count": int(row["checked_count"] or 0),
                    "messages": list(messages_by_check.get(check_name) or []),
                }
            )
        return results

    def _verification_detail_from_connection(
        self,
        conn,
        verification_id: str,
    ) -> dict[str, object]:
        row = conn.execute(
            """
            SELECT mode,pass_limit,run_config_json,error,failed_step,failed_check,failed_test,
                   sanity_status,sanity_checked_count,validation_status,validated_count
            FROM verifications
            WHERE id=?
            """,
            [verification_id],
        ).fetchone()
        if row is None:
            return {}
        sanity_check_results = self._verification_sanity_check_results(
            verification_id,
            conn=conn,
        )
        return {
            "mode": str(row["mode"] or ""),
            "pass_limit": int(row["pass_limit"] or _DETAIL_SCALAR_DEFAULTS["pass_limit"]),
            "run_config_json": str(row["run_config_json"] or ""),
            "error": str(row["error"] or ""),
            "failed_step": str(row["failed_step"] or ""),
            "failed_check": str(row["failed_check"] or ""),
            "failed_test": str(row["failed_test"] or ""),
            "sanity_status": str(row["sanity_status"] or ""),
            "sanity_checked_count": int(row["sanity_checked_count"] or 0),
            "validation_status": str(row["validation_status"] or ""),
            "validated_count": int(row["validated_count"] or 0),
            "selected_test_names": self._ordered_detail_tokens(verification_id, "verification_selected_tests", "test_name", conn=conn),
            "source_paths": self._ordered_detail_tokens(verification_id, "verification_source_paths", "source_path", conn=conn),
            "sanity_checks": [str(item.get("name") or "") for item in sanity_check_results if str(item.get("name") or "")],
            "sanity_check_results": sanity_check_results,
            "tests_meta_rows": self._verification_tests_meta_rows(verification_id, conn=conn),
        }

    def verification_detail(self, verification_id: str) -> dict[str, object]:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return {}
        with self.db.conn() as conn:
            conn.execute("BEGIN")
            return self._verification_detail_from_connection(
                conn,
                safe_verification_id,
            )

    def _replace_ordered_detail_tokens(
        self,
        conn,
        verification_id: str,
        *,
        table_name: str,
        column_name: str,
        values: list[str],
        clear_existing: bool = True,
    ) -> None:
        if clear_existing:
            conn.execute(
                f"DELETE FROM {table_name} WHERE verification_id=?",
                [verification_id],
            )
        for ordinal, token in enumerate(values, start=1):
            conn.execute(
                f"INSERT INTO {table_name}(verification_id,ordinal,{column_name}) VALUES(?,?,?)",
                [verification_id, ordinal, token],
            )

    def _normalized_sanity_check_results(self, payload: dict[str, object]) -> list[dict[str, object]]:
        raw_results = payload.get("sanity_check_results")
        results: list[dict[str, object]] = []
        if isinstance(raw_results, list):
            for raw in raw_results:
                if not isinstance(raw, dict):
                    continue
                check_name = str(raw.get("name") or raw.get("check_name") or "")
                if not check_name:
                    continue
                messages: list[dict[str, object]] = []
                for message_raw in cast(list[object], raw.get("messages") or []):
                    if not isinstance(message_raw, dict):
                        continue
                    message = str(message_raw.get("message") or "")
                    if not message:
                        continue
                    messages.append(
                        {
                            "severity": str(message_raw.get("severity") or raw.get("status") or ""),
                            "test_name": str(message_raw.get("test_name") or ""),
                            "message": message,
                        }
                    )
                results.append(
                    {
                        "name": check_name,
                        "status": str(raw.get("status") or ""),
                        "checked_count": int(raw.get("checked_count") or 0),
                        "messages": messages,
                    }
                )
            return results
        return [
            {"name": token, "status": "", "checked_count": 0, "messages": []}
            for token in [str(item or "") for item in cast(list[object], payload.get("sanity_checks") or []) if str(item or "")]
        ]

    def _replace_sanity_check_results(
        self,
        conn,
        verification_id: str,
        *,
        results: list[dict[str, object]],
        clear_existing: bool = True,
    ) -> None:
        if clear_existing:
            conn.execute(
                "DELETE FROM verification_sanity_check_messages WHERE verification_id=?",
                [verification_id],
            )
            conn.execute(
                "DELETE FROM verification_sanity_checks WHERE verification_id=?",
                [verification_id],
            )
        for ordinal, raw in enumerate(results, start=1):
            item = dict(raw)
            check_name = str(item.get("name") or "")
            if not check_name:
                continue
            conn.execute(
                """
                INSERT INTO verification_sanity_checks(verification_id,ordinal,check_name,status,checked_count)
                VALUES(?,?,?,?,?)
                """,
                [
                    verification_id,
                    ordinal,
                    check_name,
                    str(item.get("status") or ""),
                    int(item.get("checked_count") or 0),
                ],
            )
            for message_ordinal, message_raw in enumerate(cast(list[object], item.get("messages") or []), start=1):
                if not isinstance(message_raw, dict):
                    continue
                message = str(message_raw.get("message") or "")
                if not message:
                    continue
                conn.execute(
                    """
                    INSERT INTO verification_sanity_check_messages(
                        verification_id,check_name,ordinal,severity,test_name,message
                    )
                    VALUES(?,?,?,?,?,?)
                    """,
                    [
                        verification_id,
                        check_name,
                        message_ordinal,
                        str(message_raw.get("severity") or item.get("status") or ""),
                        str(message_raw.get("test_name") or ""),
                        message,
                    ],
                )

    def _replace_tests_meta_rows(
        self,
        conn,
        verification_id: str,
        *,
        selected_test_names: list[str],
        rows: list[dict[str, object]],
        clear_existing: bool = True,
    ) -> None:
        if clear_existing:
            conn.execute(
                "DELETE FROM verification_tests_meta WHERE verification_id=?",
                [verification_id],
            )
        seen_test_names: set[str] = set()
        seen_ordinals: set[int] = set()
        for position, raw in enumerate(rows, start=1):
            item = dict(raw)
            ordinal = max(1, int(item.get("index") or position))
            test_name = str(item.get("test_name") or "")
            if not test_name and item.get("index") is not None:
                test_name = f"{ordinal:03d}.in"
            if (not test_name) and position <= len(selected_test_names):
                test_name = str(selected_test_names[position - 1] or "")
            if not test_name:
                test_name = f"{ordinal:03d}.in"
            while ordinal in seen_ordinals:
                ordinal += 1
            if test_name in seen_test_names:
                continue
            seen_ordinals.add(ordinal)
            seen_test_names.add(test_name)
            conn.execute(
                """
                INSERT INTO verification_tests_meta(
                    verification_id,ordinal,test_name,source_kind,source_id,is_sample,
                    sample_input_custom,sample_output_custom,sample_output_validate,
                    description,source_path,command_text,payload_source_path
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    ordinal,
                    test_name,
                    str(item.get("kind") or ""),
                    str(item.get("id") or ""),
                    1 if bool(item.get("sample")) else 0,
                    1 if bool(item.get("sample_input_custom")) else 0,
                    1 if bool(item.get("sample_output_custom")) else 0,
                    1 if bool(item.get("sample_output_validate")) else 0,
                    str(item.get("desc") or ""),
                    str(item.get("source") or ""),
                    str(item.get("command") or ""),
                    str(item.get("payload_source") or ""),
                ],
            )

    def _write_verification_detail(
        self,
        conn,
        verification_id: str,
        detail: dict[str, object],
        *,
        clear_existing: bool = True,
    ) -> None:
        payload = dict(detail)
        scalar_values = {
            "mode": str(payload.get("mode") or _DETAIL_SCALAR_DEFAULTS["mode"]),
            "pass_limit": int(payload.get("pass_limit") or _DETAIL_SCALAR_DEFAULTS["pass_limit"]),
            "run_config_json": str(payload.get("run_config_json") or ""),
            "error": str(payload.get("error") or ""),
            "failed_step": str(payload.get("failed_step") or ""),
            "failed_check": str(payload.get("failed_check") or ""),
            "failed_test": str(payload.get("failed_test") or ""),
            "sanity_status": str(payload.get("sanity_status") or ""),
            "sanity_checked_count": int(payload.get("sanity_checked_count") or 0),
            "validation_status": str(payload.get("validation_status") or ""),
            "validated_count": int(payload.get("validated_count") or 0),
        }
        selected_test_names = [str(item or "") for item in cast(list[object], payload.get("selected_test_names") or []) if str(item or "")]
        source_paths = [str(item or "") for item in cast(list[object], payload.get("source_paths") or []) if str(item or "")]
        sanity_check_results = self._normalized_sanity_check_results(payload)
        tests_meta_rows = [dict(item) for item in cast(list[object], payload.get("tests_meta_rows") or []) if isinstance(item, dict)]

        conn.execute(
            """
            UPDATE verifications
            SET mode=?,pass_limit=?,run_config_json=?,error=?,failed_step=?,failed_check=?,failed_test=?,
                sanity_status=?,sanity_checked_count=?,validation_status=?,validated_count=?
            WHERE id=?
            """,
            [
                scalar_values["mode"],
                scalar_values["pass_limit"],
                scalar_values["run_config_json"],
                scalar_values["error"],
                scalar_values["failed_step"],
                scalar_values["failed_check"],
                scalar_values["failed_test"],
                scalar_values["sanity_status"],
                scalar_values["sanity_checked_count"],
                scalar_values["validation_status"],
                scalar_values["validated_count"],
                verification_id,
            ],
        )
        self._replace_ordered_detail_tokens(
            conn,
            verification_id,
            table_name="verification_selected_tests",
            column_name="test_name",
            values=selected_test_names,
            clear_existing=clear_existing,
        )
        self._replace_ordered_detail_tokens(
            conn,
            verification_id,
            table_name="verification_source_paths",
            column_name="source_path",
            values=source_paths,
            clear_existing=clear_existing,
        )
        self._replace_sanity_check_results(
            conn,
            verification_id,
            results=sanity_check_results,
            clear_existing=clear_existing,
        )
        self._replace_tests_meta_rows(
            conn,
            verification_id,
            selected_test_names=selected_test_names,
            rows=tests_meta_rows,
            clear_existing=clear_existing,
        )

    def store_verification_blob(
        self,
        *,
        verification_id: str,
        test_name: str,
        role: str,
        file_name: str,
        payload: bytes,
        extra_tags: dict[str, object] | None = None,
    ) -> str:
        _ = verification_id, test_name, role, file_name, extra_tags
        return self.runtime_blob_store.put_bytes(payload).blob_ref or ""

    def verification_artifact_refs(self, verification_id: str) -> dict[str, dict[str, str]]:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return {}
        rows = self.db.fetch_all(
            """
            SELECT test_name,input_ref,answer_ref
            FROM verification_artifact_refs
            WHERE verification_id=?
            ORDER BY test_name ASC
            """,
            [safe_verification_id],
        )
        refs: dict[str, dict[str, str]] = {}
        for row in rows:
            test_name = str(row["test_name"] or "")
            if not test_name:
                continue
            item: dict[str, str] = {}
            input_ref = str(row["input_ref"] or "")
            answer_ref = str(row["answer_ref"] or "")
            if input_ref:
                item["input_ref"] = input_ref
            if answer_ref:
                item["answer_ref"] = answer_ref
            if item:
                refs[test_name] = item
        return refs

    def verification_artifact_ref(self, verification_id: str, test_name: str, ref_key: str) -> str:
        safe_ref_key = str(ref_key or "").strip()
        if safe_ref_key not in {"input_ref", "answer_ref"}:
            return ""
        row = self.db.fetch_one(
            """
            SELECT input_ref,answer_ref
            FROM verification_artifact_refs
            WHERE verification_id=? AND test_name=?
            """,
            [
                str(verification_id or "").strip(),
                str(test_name or "").strip(),
            ],
        )
        if row is None:
            return ""
        return str(row[safe_ref_key] or "")

    def verification_task_output_ref(self, verification_id: str, task_id: str) -> tuple[str, str] | None:
        row = self.db.fetch_one(
            """
            SELECT test_name,result_json
            FROM verification_tasks
            WHERE verification_id=? AND id=?
            """,
            [str(verification_id or "").strip(), str(task_id or "").strip()],
        )
        if row is None:
            return None
        result = execution_result_from_json(str(row["result_json"] or "{}"))
        return (str(row["test_name"] or ""), result.output_run_ref)

    def workspace_verification_run_ids(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> list[str] | None:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return None
        if not self.workspace_verification_exists(int(problem_id), int(workspace_id), safe_verification_id):
            return None
        values: list[str] = []
        for row in self.task_store.list_rows(safe_verification_id):
            run_id = str(row["run_id"] or "")
            if run_id and run_id not in values:
                values.append(run_id)
        return values

    def verification_artifact_tokens(self, verification_id: str) -> set[str]:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return set()
        tokens: set[str] = set()
        for refs in self.verification_artifact_refs(safe_verification_id).values():
            input_ref = str(refs.get("input_ref") or "")
            answer_ref = str(refs.get("answer_ref") or "")
            if input_ref:
                tokens.add(input_ref)
            if answer_ref:
                tokens.add(answer_ref)
        for row in self.task_store.list_rows(safe_verification_id):
            tokens.update(row["result"].artifact_refs())
        return tokens

    def verification_has_artifact_token(self, verification_id: str, token: str) -> bool:
        safe_verification_id = str(verification_id or "").strip()
        safe_token = str(token or "").strip()
        if (not safe_verification_id) or (not safe_token):
            return False
        return safe_token in self.verification_artifact_tokens(safe_verification_id)

    def resolve_artifact_blob(self, token: str) -> bytes | None:
        return self.judgehost_task_service.resolve_artifact_blob(token)

    def artifact_descriptor(self, token: str) -> PayloadFile | None:
        return self.runtime_blob_store.descriptor(token)

    @staticmethod
    def verification_runtime_summary_from_tasks(
        rows: list[dict[str, object]],
    ) -> dict[str, object]:
        counts = task_counts(rows)
        return {
            "task_graph": bool(rows),
            "task_counts": counts,
            "running_tasks": running_tasks(rows),
            "source_paths": solution_source_paths(rows),
            "program_ids": program_ids(rows),
            "has_running": bool(int(counts["pending"]) or int(counts["queued"]) or int(counts["running"])),
            "test_names": list(dict.fromkeys(str(row["test_name"] or "") for row in rows if str(row["test_name"] or ""))),
        }

    def verification_runtime_summary(self, verification_id: str) -> dict[str, object]:
        snapshot = self.verification_snapshot(verification_id)
        return self.verification_runtime_summary_from_tasks(
            [] if snapshot is None else snapshot["tasks"]
        )

    def verification_source_paths(self, verification_id: str) -> list[str]:
        detail = self.verification_detail(verification_id)
        return list(cast(list[str], detail.get("source_paths") or []))

    def list_visible_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.ALL, Kind.SAMPLE, Kind.CUSTOM),
    ) -> list[dict[str, object]]:
        rows = self._verification_store.list_visible_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=int(limit),
            kinds=kinds,
        )
        return [dict(row) for row in rows]

    def visible_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.ALL.value,),
    ) -> list[WorkspaceVerificationRow]:
        return self._verification_store.visible_verification_rows(
            int(problem_id),
            int(workspace_id),
            limit=max(1, int(limit)),
            kinds=kinds,
        )

    def visible_verification_rows_many(
        self,
        subjects: list[WorkspaceVerificationKey],
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.ALL.value,),
    ) -> dict[WorkspaceVerificationKey, list[WorkspaceVerificationRow]]:
        return self._verification_store.visible_verification_rows_many(
            subjects,
            limit=max(1, int(limit)),
            kinds=kinds,
        )

    def workspace_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.ALL.value,),
    ) -> list[WorkspaceVerificationRow]:
        return self._verification_store.workspace_verification_rows(
            int(problem_id),
            int(workspace_id),
            limit=max(1, int(limit)),
            kinds=kinds,
        )

    def workspace_verification_rows_many(
        self,
        subjects: list[WorkspaceVerificationKey],
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.ALL.value,),
    ) -> dict[WorkspaceVerificationKey, list[WorkspaceVerificationRow]]:
        return self._verification_store.workspace_verification_rows_many(
            subjects,
            limit=max(1, int(limit)),
            kinds=kinds,
        )

    def latest_workspace_verification(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        ok_only: bool = False,
    ) -> WorkspaceVerificationRow | None:
        rows = self._verification_store.workspace_verification_rows(
            int(problem_id),
            int(workspace_id),
            limit=40,
            ok_only=bool(ok_only),
        )
        return rows[0] if rows else None

    def workspace_source_commit_verification(
        self,
        problem_id: int,
        workspace_id: int,
        source_commit: str,
        *,
        kinds: tuple[str, ...] = (Kind.ALL.value, Kind.CUSTOM.value),
        ok_only: bool = False,
    ) -> WorkspaceVerificationRow | None:
        return self._verification_store.workspace_source_commit_verification_row(
            int(problem_id),
            int(workspace_id),
            source_commit,
            kinds=kinds,
            ok_only=bool(ok_only),
        )

    def admit_verification(
        self,
        request: VerificationAdmission,
    ) -> AdmissionCommit:
        canonical_verification_id(request.verification_id)
        return self._verification_store.admit(request)

    def activate_verification(
        self,
        plan: ActivationPlan,
    ) -> ActivationCommit:
        canonical_verification_id(plan.verification_id)
        return self.task_store.activate_plan(
            plan,
            write_detail=lambda conn, verification_id, detail: (
                self._write_verification_detail(
                    conn,
                    verification_id,
                    detail,
                    clear_existing=False,
                )
            ),
        )

    def fail_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationTransitionCommit:
        canonical_verification_id(verification_id)
        return self.task_store.transition_verification_failed(
            verification_id,
            reason=reason,
        )

    def cancel_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationTransitionCommit:
        return self.fail_verification(verification_id, reason=reason)

    def finish_sanity(
        self,
        finish: SanityFinish,
    ) -> VerificationTransitionCommit:
        canonical_verification_id(finish.verification_id)
        return self.task_store.finish_sanity(
            finish,
            write_detail=self._write_verification_detail,
        )

    def recover_startup(
        self,
        *,
        reason: str = "cancelled on service startup",
    ) -> StartupRecoverySummary:
        return self.task_store.recover_startup(reason=reason)

    def verification_snapshot(
        self,
        verification_id: str,
    ) -> VerificationSnapshot | None:
        try:
            canonical_verification_id(verification_id)
        except RuntimeError:
            return None

        def _read(conn) -> VerificationSnapshot | None:
            row = conn.execute(
                """
                SELECT id,problem_id,workspace_id,signature,source_commit,kind,
                       status,fail_reason,created_at,finished_at
                FROM verifications
                WHERE id=?
                """,
                [verification_id],
            ).fetchone()
            if row is None:
                return None
            workspace_id = row["workspace_id"]
            record: VerificationSnapshotRecord = {
                "id": str(row["id"] or ""),
                "problem_id": int(row["problem_id"]),
                "workspace_id": (
                    None if workspace_id is None else int(workspace_id)
                ),
                "signature": str(row["signature"] or ""),
                "source_commit": str(row["source_commit"] or ""),
                "kind": str(row["kind"] or ""),
                "status": str(row["status"] or ""),
                "fail_reason": str(row["fail_reason"] or ""),
                "created_at": str(row["created_at"] or ""),
                "finished_at": str(row["finished_at"] or ""),
            }
            return {
                "record": record,
                "detail": self._verification_detail_from_connection(
                    conn,
                    verification_id,
                ),
                "tasks": self.task_store.snapshot_rows(conn, verification_id),
            }

        return self.task_store.read_lifecycle_snapshot(_read)

    def _select_checker_source(
        self,
        snapshot: Path,
        build_cfg: BuildConfig,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        return select_source(
            snapshot,
            build_cfg,
            "checker_source",
            "checkers",
            cpp_extensions=CPP_EXTENSIONS,
            snapshot_resolved=snapshot_resolved,
        )

    def _select_source(
        self,
        snapshot: Path,
        build_cfg: BuildConfig,
        config_key: str,
        folder: str,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        return select_source(
            snapshot=snapshot,
            build_cfg=build_cfg,
            config_key=config_key,
            folder=folder,
            cpp_extensions=CPP_EXTENSIONS,
            snapshot_resolved=snapshot_resolved,
        )

    def _manual_test_sources(self, snapshot: Path) -> list[Path]:
        return manual_test_sources(snapshot)

    def _prepare_tests_spec_runtime(
        self,
        snapshot: Path,
        tests_spec_entries: list[TestSpecEntry],
        *,
        generator_sources: list[str],
    ) -> tuple[list[dict], list[tuple[str, Path]]]:
        return prepare_tests_spec_runtime(
            snapshot,
            tests_spec_entries,
            generator_sources=generator_sources,
            generator_source_extensions=GENERATOR_SOURCE_EXTENSIONS,
            parse_gen_command_tokens_fn=parse_gen_command_tokens,
        )

    def run_verification(
        self,
        problem: str,
        username: str,
        commit: str | None = None,
        *,
        sample_only: bool = False,
        verification_id: str = "",
    ) -> str:
        from app.impl.workspace.verification_dag import run_workspace_verification_dag

        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace_path = Path(str(ctx["workspace"]["path"])).resolve()
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(self.workspace_service.global_user_context(username)["id"])
        status = self.workspace_service.read_workspace_status(workspace_path)
        workspace_head = str(status.get("head_commit") or "")
        workspace_dirty = bool(status.get("dirty"))
        snapshot_root: Path | None = None
        source_commit = ""
        if commit:
            source_commit = self.workspace_service.resolve_commit(workspace_path, commit)
            snapshot_root = self.workspace_service.create_snapshot(workspace_path, source_commit)
            workspace_dirty = False
        else:
            snapshot_root = self.workspace_service.create_snapshot(
                workspace_path,
                None,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
            )
        assert snapshot_root is not None
        manifest = verification_manifest(snapshot_root)
        signature = manifest.signature
        target_verification_id = (
            canonical_verification_id(verification_id)
            if verification_id
            else new_verification_id()
        )
        admission = self.admit_verification(
            VerificationAdmission(
                verification_id=target_verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                signature=signature,
                source_commit=source_commit,
                kind=Kind.SAMPLE.value if sample_only else Kind.ALL.value,
            )
        )
        if admission.outcome != "admitted":
            raise RuntimeError(
                f"verification already exists: {target_verification_id}"
            )
        run_workspace_verification_dag(
            problem,
            username,
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_head=source_commit or workspace_head,
            workspace_dirty=workspace_dirty,
            targets=[],
            verification_id=target_verification_id,
            signature=signature,
            source_commit=source_commit,
            kind=Kind.SAMPLE.value if sample_only else Kind.ALL.value,
            sample_only=bool(sample_only),
            snapshot_root_override=snapshot_root,
            manifest=manifest,
        )
        return target_verification_id
