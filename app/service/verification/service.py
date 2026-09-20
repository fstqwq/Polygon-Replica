import sqlite3
from collections.abc import Callable, Mapping

from app.config import ConfigValues
from app.db import DB
from app.service.disk.verification_store import VerificationStore
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.platform.fs.layout import StorageLayout
from app.service.repository.workspace import WorkspaceService
from app.service.verification.types import (
    Kind,
    VerificationDetail,
    VerificationDetailEnvelope,
    VerificationSanityCheckRow,
    VerificationSanityMessageRow,
    VerificationTestMetadata,
    VerificationRecordRow,
    VerificationStatus,
    WorkspaceVerificationKey,
    WorkspaceVerificationRow,
)
from app.service.verification.artifact import (
    VerificationArtifact,
    VerificationArtifactQuery,
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
    VerificationRuntimeSummary,
    program_ids,
    running_tasks,
    solution_source_paths,
    task_counts,
)
from app.service.verification.detail_read_model import (
    VerificationDetailReadModel,
    VerificationTestDetailReadModel,
    build_verification_detail_read_model,
    build_verification_test_detail_read_model,
)
from app.service.verification.types import VerificationTaskRow
from app.service.verification.task_store import VerificationTaskStore

from app.service.judgehost.api import Judgehost


def _payload_int(payload: Mapping[str, object], key: str, *, default: int) -> int:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"verification detail {key} must be an integer")
    return value


def _payload_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"verification detail {key} must be a list")
    return value


class VerificationService:
    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        judgehost_task_service: Judgehost,
        task_store: VerificationTaskStore,
        runtime_blob_store: RuntimeBlobStore,
        storage_layout: StorageLayout,
        config_values: ConfigValues,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.judgehost_task_service = judgehost_task_service
        self.task_store = task_store
        self.runtime_blob_store = runtime_blob_store
        self.storage_layout = storage_layout
        self._config_values = config_values
        self._verification_store = VerificationStore(db)
        self._artifact_query = VerificationArtifactQuery(db, runtime_blob_store)

    def allocate_verification_id(self) -> str:
        return new_verification_id()

    def workspace_verification_detail(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> VerificationDetailEnvelope | None:
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

    def verification_record(self, verification_id: str) -> VerificationRecordRow | None:
        row = self._verification_store.record_row(verification_id)
        return row

    def _ordered_detail_tokens(
        self,
        verification_id: str,
        table_name: str,
        column_name: str,
        *,
        conn: sqlite3.Connection | None = None,
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
        conn: sqlite3.Connection | None = None,
    ) -> list[VerificationTestMetadata]:
        sql = """
            SELECT ordinal,test_name,source_kind,source_id,is_sample,sample_input_custom,sample_output_custom,
                   sample_output_validate,description,source_path,command_text,payload_source_path
            FROM verification_tests_meta
            WHERE verification_id=?
            ORDER BY ordinal ASC
            """
        params = [str(verification_id or "").strip()]
        rows = self.db.fetch_all(sql, params) if conn is None else conn.execute(sql, params).fetchall()
        values: list[VerificationTestMetadata] = []
        for row in rows:
            item: VerificationTestMetadata = {
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
        conn: sqlite3.Connection | None = None,
    ) -> list[VerificationSanityCheckRow]:
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
        messages_by_check: dict[str, list[VerificationSanityMessageRow]] = {}
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
        results: list[VerificationSanityCheckRow] = []
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
        conn: sqlite3.Connection,
        verification_id: str,
    ) -> VerificationDetail:
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
            "pass_limit": int(row["pass_limit"] or 1),
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

    def verification_detail(self, verification_id: str) -> VerificationDetail:
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
        conn: sqlite3.Connection,
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

    def _normalized_sanity_check_results(self, payload: Mapping[str, object]) -> list[VerificationSanityCheckRow]:
        raw_results = payload.get("sanity_check_results")
        results: list[VerificationSanityCheckRow] = []
        if isinstance(raw_results, list):
            for raw in raw_results:
                if not isinstance(raw, dict):
                    continue
                check_name = str(raw.get("name") or raw.get("check_name") or "")
                if not check_name:
                    continue
                messages: list[VerificationSanityMessageRow] = []
                for message_raw in _payload_list(raw, "messages"):
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
                        "checked_count": _payload_int(raw, "checked_count", default=0),
                        "messages": messages,
                    }
                )
            return results
        return [
            {"name": token, "status": "", "checked_count": 0, "messages": []}
            for token in [str(item or "") for item in _payload_list(payload, "sanity_checks") if str(item or "")]
        ]

    def _replace_sanity_check_results(
        self,
        conn: sqlite3.Connection,
        verification_id: str,
        *,
        results: list[VerificationSanityCheckRow],
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
        for ordinal, item in enumerate(results, start=1):
            check_name = item["name"]
            conn.execute(
                """
                INSERT INTO verification_sanity_checks(verification_id,ordinal,check_name,status,checked_count)
                VALUES(?,?,?,?,?)
                """,
                [
                    verification_id,
                    ordinal,
                    check_name,
                    item["status"],
                    item["checked_count"],
                ],
            )
            for message_ordinal, message in enumerate(item["messages"], start=1):
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
                        message["severity"] or item["status"],
                        message["test_name"],
                        message["message"],
                    ],
                )

    def _replace_tests_meta_rows(
        self,
        conn: sqlite3.Connection,
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
            ordinal = max(1, _payload_int(item, "index", default=position))
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
        conn: sqlite3.Connection,
        verification_id: str,
        detail: Mapping[str, object],
        *,
        clear_existing: bool = True,
    ) -> None:
        payload = dict(detail)
        scalar_values = {
            "mode": str(payload.get("mode") or "pass-fail"),
            "pass_limit": _payload_int(payload, "pass_limit", default=1),
            "run_config_json": str(payload.get("run_config_json") or ""),
            "error": str(payload.get("error") or ""),
            "failed_step": str(payload.get("failed_step") or ""),
            "failed_check": str(payload.get("failed_check") or ""),
            "failed_test": str(payload.get("failed_test") or ""),
            "sanity_status": str(payload.get("sanity_status") or ""),
            "sanity_checked_count": _payload_int(
                payload, "sanity_checked_count", default=0
            ),
            "validation_status": str(payload.get("validation_status") or ""),
            "validated_count": _payload_int(payload, "validated_count", default=0),
        }
        selected_test_names = [str(item or "") for item in _payload_list(payload, "selected_test_names") if str(item or "")]
        source_paths = [str(item or "") for item in _payload_list(payload, "source_paths") if str(item or "")]
        sanity_check_results = self._normalized_sanity_check_results(payload)
        tests_meta_rows = [dict(item) for item in _payload_list(payload, "tests_meta_rows") if isinstance(item, dict)]

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

    def verification_test_artifacts(self, verification_id: str) -> dict[str, dict[str, str]]:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return {}
        rows = self.db.fetch_all(
            """
            SELECT test_name,role,artifact_ref
            FROM verification_task_artifacts
            WHERE verification_id=?
              AND role IN ('generated-input','accepted-answer')
            ORDER BY test_name,task_id
            """,
            [safe_verification_id],
        )
        refs: dict[str, dict[str, str]] = {}
        for row in rows:
            test_name = str(row["test_name"] or "")
            if not test_name:
                continue
            item = refs.setdefault(test_name, {})
            role = str(row["role"])
            key = "input_ref" if role == "generated-input" else "answer_ref"
            item.setdefault(key, str(row["artifact_ref"]))
        return refs

    def verification_artifact_ref(self, verification_id: str, test_name: str, ref_key: str) -> str:
        safe_ref_key = str(ref_key or "").strip()
        roles = {
            "input_ref": "generated-input",
            "answer_ref": "accepted-answer",
        }
        if safe_ref_key not in roles:
            return ""
        row = self.db.fetch_one(
            """
            SELECT artifact_ref
            FROM verification_task_artifacts
            WHERE verification_id=? AND test_name=? AND role=?
            ORDER BY task_id
            LIMIT 1
            """,
            [
                str(verification_id or "").strip(),
                str(test_name or "").strip(),
                roles[safe_ref_key],
            ],
        )
        if row is None:
            return ""
        return str(row["artifact_ref"] or "")

    def verification_artifact(
        self,
        verification_id: str,
        virtual_path: str,
    ) -> VerificationArtifact | None:
        return self._artifact_query.resolve(verification_id, virtual_path)

    def artifact_descriptor(self, token: str) -> PayloadFile | None:
        return self.runtime_blob_store.descriptor(token)

    @staticmethod
    def verification_runtime_summary_from_tasks(
        rows: list[VerificationTaskRow],
    ) -> VerificationRuntimeSummary:
        counts = task_counts(rows)
        return {
            "task_graph": bool(rows),
            "task_counts": counts,
            "running_tasks": running_tasks(rows),
            "source_paths": solution_source_paths(rows),
            "program_ids": program_ids(rows),
            "has_running": bool(
                counts["pending"] or counts["queued"] or counts["running"]
            ),
            "test_names": list(dict.fromkeys(str(row["test_name"] or "") for row in rows if str(row["test_name"] or ""))),
        }

    def verification_source_paths(self, verification_id: str) -> list[str]:
        detail = self.verification_detail(verification_id)
        return list(detail.get("source_paths") or [])

    def list_visible_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int = 40,
    ) -> list[VerificationRecordRow]:
        return self._verification_store.list_visible_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=int(limit),
        )

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
        return self.task_store.transition_verification_terminal(
            verification_id, status=VerificationStatus.FAILED, reason=reason
        )

    def cancel_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationTransitionCommit:
        canonical_verification_id(verification_id)
        return self.task_store.transition_verification_terminal(
            verification_id, status=VerificationStatus.CANCELLED, reason=reason
        )

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
        reason: str = "interrupted by application restart",
    ) -> StartupRecoverySummary:
        return self.task_store.recover_startup(reason=reason)

    def verification_snapshot(
        self,
        verification_id: str,
        *,
        authorize: Callable[[VerificationSnapshotRecord], None] | None = None,
        test_name: str | None = None,
        program_id: str | None = None,
    ) -> VerificationSnapshot | None:
        try:
            canonical_verification_id(verification_id)
        except RuntimeError:
            return None

        def _read(conn: sqlite3.Connection) -> VerificationSnapshot | None:
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
                "status": VerificationStatus(str(row["status"])),
                "fail_reason": str(row["fail_reason"] or ""),
                "created_at": str(row["created_at"] or ""),
                "finished_at": str(row["finished_at"] or ""),
            }
            if authorize is not None:
                authorize(record)
            return {
                "record": record,
                "detail": self._verification_detail_from_connection(
                    conn,
                    verification_id,
                ),
                "tasks": self.task_store.snapshot_rows(
                    conn, verification_id, test_name=test_name, program_id=program_id,
                ),
            }

        return self.task_store.read_lifecycle_snapshot(_read)

    def verification_detail_read_model(
        self,
        verification_id: str,
        *,
        include_pass_details: bool = True,
    ) -> VerificationDetailReadModel | None:
        snapshot = self.verification_snapshot(verification_id)
        if snapshot is None:
            return None
        return build_verification_detail_read_model(
            snapshot,
            include_pass_details=include_pass_details,
            display_limit=self._config_values.integer(
                "AUX_DISPLAY_TEXT_LIMIT_BYTES"
            ),
        )

    def verification_test_detail_read_model(
        self,
        verification_id: str,
        *,
        test_name: str,
        program_id: str | None = None,
        authorize: Callable[[VerificationSnapshotRecord], None],
    ) -> VerificationTestDetailReadModel | None:
        if not test_name:
            raise ValueError("test_name is required")
        snapshot = self.verification_snapshot(
            verification_id, authorize=authorize, test_name=test_name,
            program_id=program_id,
        )
        if snapshot is None:
            return None
        return build_verification_test_detail_read_model(
            snapshot, test_name=test_name, program_id=program_id,
        )
