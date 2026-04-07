from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import cast

from app.db import DB, now_iso
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.artifact import ArtifactService
from app.service.disk.verification_store import VerificationStore
from app.service.platform.fs.layout import FsManager
from app.service.platform.hashing import sha256_hex_bytes, sha256_hex_json
from app.service.platform.judge_fs_index import JudgeFsIndexService
from app.service.judgehost.domjudge.cache import domjudge_cache_blob_ref
from app.service.problem.test_spec import (
    parse_gen_command_tokens,
)
from app.service.repository.workspace import WorkspaceService
from app.service.verification.types import Kind

from app.service.verification.read_model import (
    logical_run_ids,
    running_tasks,
    solution_source_paths,
    task_counts,
)
from app.service.verification.runtime import load_problem_runtime_config
from app.service.verification.signature import verification_signature
from app.service.verification.source import resolve_standard_checker_source, select_checker_source, select_source
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.test_spec import load_tests_spec_entries, manual_test_sources, prepare_tests_spec_runtime

from app.service.judgehost.api import Judgehost


CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
GENERATOR_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_TEST_NAME_RE = re.compile(r"^[0-9]{3}\.in$")
STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[3] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
DEFAULT_TIME_LIMIT_MS = 2000
TIME_LIMIT_MIN_MS = 100
TIME_LIMIT_MAX_MS = 30000

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
        artifacts: ArtifactService,
        judgehost_task_service: Judgehost,
        judge_fs_index_service: JudgeFsIndexService | None = None,
        constants: RuntimeValues | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.judgehost_task_service = judgehost_task_service
        self.judge_fs_index_service = judge_fs_index_service
        self._verification_inflight_lock = threading.RLock()
        self.fs_manager = FsManager(self.workspace_service.settings.cache_root, self.workspace_service.settings.artifacts_root)
        self._verification_store = VerificationStore(db, self.fs_manager)
        self.apply_runtime_values(constants or build_runtime_values())

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
        return row["status"] in {"queued", "pending", "running", "ok", "failed"}

    def artifact_path_for_problem_artifact(self, problem_id: int, artifact_id: str) -> str:
        return self._verification_store.artifact_path_for_problem_artifact(int(problem_id), artifact_id)

    def artifact_path_for_verification(self, verification_id: str) -> str:
        return self._verification_store.artifact_path_for_verification(verification_id)

    def allocate_verification_id(self) -> str:
        return self._verification_store.allocate_id()

    def workspace_verification_id_for_run(self, problem_id: int, workspace_id: int, run_id: str) -> str:
        token = run_id
        if not token:
            return ""
        task_store = VerificationTaskStore(self.db)
        for row in self._verification_store.list_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=512,
            kinds=(Kind.ALL, Kind.SAMPLE, Kind.CUSTOM),
        ):
            verification_id = str(row["id"])
            for task_row in task_store.list_rows(verification_id):
                if str(task_row["logical_run_id"] or "") == token:
                    return verification_id
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
        row = self._verification_store.workspace_verification_row(int(problem_id), int(workspace_id), verification_id)
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "status": str(row["status"] or ""),
            "details": self.verification_detail(verification_id),
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

    def _ordered_detail_tokens(self, verification_id: str, table_name: str, column_name: str) -> list[str]:
        rows = self.db.fetch_all(
            f"""
            SELECT {column_name}
            FROM {table_name}
            WHERE verification_id=?
            ORDER BY ordinal ASC
            """,
            [str(verification_id or "").strip()],
        )
        values: list[str] = []
        for row in rows:
            token = str(row[column_name] or "")
            if token:
                values.append(token)
        return values

    def _verification_tests_meta_rows(self, verification_id: str) -> list[dict[str, object]]:
        rows = self.db.fetch_all(
            """
            SELECT ordinal,test_name,source_kind,source_id,is_sample,sample_input_custom,sample_output_custom,
                   sample_output_validate,description,source_path,command_text,payload_source_path
            FROM verification_tests_meta
            WHERE verification_id=?
            ORDER BY ordinal ASC
            """,
            [str(verification_id or "").strip()],
        )
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

    def verification_detail(self, verification_id: str) -> dict[str, object]:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return {}
        row = self.db.fetch_one(
            """
            SELECT mode,pass_limit,run_config_json,error,failed_step,failed_check,failed_test,
                   sanity_status,sanity_checked_count,validation_status,validated_count
            FROM verifications
            WHERE id=?
            """,
            [safe_verification_id],
        )
        if row is None:
            return {}
        return {
            "mode": str(row["mode"] or _DETAIL_SCALAR_DEFAULTS["mode"]),
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
            "selected_test_names": self._ordered_detail_tokens(safe_verification_id, "verification_selected_tests", "test_name"),
            "source_paths": self._ordered_detail_tokens(safe_verification_id, "verification_source_paths", "source_path"),
            "sanity_checks": self._ordered_detail_tokens(safe_verification_id, "verification_sanity_checks", "check_name"),
            "tests_meta_rows": self._verification_tests_meta_rows(safe_verification_id),
        }

    def _replace_ordered_detail_tokens(
        self,
        conn,
        verification_id: str,
        *,
        table_name: str,
        column_name: str,
        values: list[str],
    ) -> None:
        conn.execute(f"DELETE FROM {table_name} WHERE verification_id=?", [verification_id])
        for ordinal, token in enumerate(values, start=1):
            conn.execute(
                f"INSERT INTO {table_name}(verification_id,ordinal,{column_name}) VALUES(?,?,?)",
                [verification_id, ordinal, token],
            )

    def _replace_tests_meta_rows(
        self,
        conn,
        verification_id: str,
        *,
        selected_test_names: list[str],
        rows: list[dict[str, object]],
    ) -> None:
        conn.execute("DELETE FROM verification_tests_meta WHERE verification_id=?", [verification_id])
        for position, raw in enumerate(rows, start=1):
            item = dict(raw)
            ordinal = max(1, int(item.get("index") or position))
            test_name = str(item.get("test_name") or "")
            if (not test_name) and position <= len(selected_test_names):
                test_name = str(selected_test_names[position - 1] or "")
            if not test_name:
                test_name = f"{ordinal:03d}.in"
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

    def persist_verification_detail(self, verification_id: str, detail: dict[str, object]) -> None:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return
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
        sanity_checks = [str(item or "") for item in cast(list[object], payload.get("sanity_checks") or []) if str(item or "")]
        tests_meta_rows = [dict(item) for item in cast(list[object], payload.get("tests_meta_rows") or []) if isinstance(item, dict)]

        def _tx(conn) -> None:
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
                    safe_verification_id,
                ],
            )
            self._replace_ordered_detail_tokens(
                conn,
                safe_verification_id,
                table_name="verification_selected_tests",
                column_name="test_name",
                values=selected_test_names,
            )
            self._replace_ordered_detail_tokens(
                conn,
                safe_verification_id,
                table_name="verification_source_paths",
                column_name="source_path",
                values=source_paths,
            )
            self._replace_ordered_detail_tokens(
                conn,
                safe_verification_id,
                table_name="verification_sanity_checks",
                column_name="check_name",
                values=sanity_checks,
            )
            self._replace_tests_meta_rows(
                conn,
                safe_verification_id,
                selected_test_names=selected_test_names,
                rows=tests_meta_rows,
            )

        self.db.write_transaction(_tx)

    def _verification_blob_ref(self, *, key_hash: str, signature: str, name: str) -> str:
        return domjudge_cache_blob_ref(
            kind=JudgeFsIndexService.KIND_VERIFICATION,
            key_hash=key_hash,
            signature=signature,
            name=name,
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
        service = self.judge_fs_index_service
        if service is None:
            raise RuntimeError("verification blob store is unavailable")
        safe_verification_id = str(verification_id or "").strip()
        safe_test_name = str(test_name or "").strip()
        safe_role = str(role or "").strip().lower()
        safe_file_name = Path(file_name).name
        blob = bytes(payload)
        key_hash = sha256_hex_json(
            {
                "schema": "verification-artifact-key.v1",
                "verification_id": safe_verification_id,
                "test_name": safe_test_name,
                "role": safe_role,
            },
            ensure_ascii=False,
        )
        signature = JudgeFsIndexService.signature(
            {
                "schema": "verification-artifact.v1",
                "payload_sha256": sha256_hex_bytes(blob),
                "file_name": safe_file_name,
            }
        )
        tags = {
            "verification_id": safe_verification_id,
            "test_name": safe_test_name,
            "role": safe_role,
        }
        if extra_tags:
            tags.update(dict(extra_tags))
        service.put(
            kind=JudgeFsIndexService.KIND_VERIFICATION,
            key_hash=key_hash,
            signature=signature,
            value={
                "schema": "verification-artifact.v1",
                "verification_id": safe_verification_id,
                "test_name": safe_test_name,
                "role": safe_role,
                "file_name": safe_file_name,
                "payload_sha256": sha256_hex_bytes(blob),
            },
            files={safe_file_name: blob},
            tags=tags,
        )
        return self._verification_blob_ref(key_hash=key_hash, signature=signature, name=safe_file_name)

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
            SELECT test_name,output_ref
            FROM verification_tasks
            WHERE verification_id=? AND id=?
            """,
            [str(verification_id or "").strip(), str(task_id or "").strip()],
        )
        if row is None:
            return None
        return (str(row["test_name"] or ""), str(row["output_ref"] or ""))

    def resolve_artifact_blob(self, token: str) -> bytes | None:
        return self.judgehost_task_service.resolve_artifact_blob(token)

    def update_verification_artifact_refs(self, verification_id: str, test_name: str, refs: dict[str, str]) -> dict[str, object]:
        safe_verification_id = str(verification_id or "").strip()
        safe_test_name = str(test_name or "").strip()
        if (not safe_verification_id) or (not safe_test_name):
            return {}
        normalized = {
            str(key): str(value)
            for key, value in dict(refs).items()
            if str(key) in {"input_ref", "answer_ref"} and str(value or "")
        }
        if not normalized:
            return self.verification_artifact_refs(safe_verification_id)
        with self._verification_inflight_lock:
            current_row = self.db.fetch_one(
                """
                SELECT input_ref,answer_ref
                FROM verification_artifact_refs
                WHERE verification_id=? AND test_name=?
                """,
                [safe_verification_id, safe_test_name],
            )
            input_ref = str((current_row["input_ref"] if current_row is not None else "") or "")
            answer_ref = str((current_row["answer_ref"] if current_row is not None else "") or "")
            if "input_ref" in normalized:
                input_ref = normalized["input_ref"]
            if "answer_ref" in normalized:
                answer_ref = normalized["answer_ref"]
            self.db.execute(
                """
                INSERT INTO verification_artifact_refs(verification_id,test_name,input_ref,answer_ref,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(verification_id,test_name) DO UPDATE SET
                    input_ref=excluded.input_ref,
                    answer_ref=excluded.answer_ref,
                    updated_at=excluded.updated_at
                """,
                [safe_verification_id, safe_test_name, input_ref, answer_ref, now_iso()],
            )
        return self.verification_artifact_refs(safe_verification_id)


    def verification_runtime_summary(self, verification_id: str) -> dict[str, object]:
        rows = VerificationTaskStore(self.db).list_rows_for_list(verification_id)
        counts = task_counts(rows)
        return {
            "task_graph": bool(rows),
            "task_counts": counts,
            "running_tasks": running_tasks(rows),
            "source_paths": solution_source_paths(rows),
            "logical_run_ids": logical_run_ids(rows, include_main_correct=True),
            "has_running": bool(int(counts["pending"]) or int(counts["queued"]) or int(counts["running"])),
            "test_names": [str(row["test_name"] or "") for row in rows if str(row["test_name"] or "")],
        }

    def verification_run_ids(self, verification_id: str) -> list[str]:
        summary = self.verification_runtime_summary(verification_id)
        return list(summary["logical_run_ids"])

    def verification_source_paths(self, verification_id: str) -> list[str]:
        detail = self.verification_detail(verification_id)
        return list(cast(list[str], detail.get("source_paths") or []))

    def list_workspace_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.ALL, Kind.SAMPLE, Kind.CUSTOM),
    ) -> list[dict[str, object]]:
        rows = self._verification_store.list_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=int(limit),
            kinds=kinds,
        )
        return [dict(row) for row in rows]

    def begin_verification_record(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int | None,
        signature: str = "",
        kind: str,
        status: str,
        detail: dict[str, object] | None = None,
    ) -> str:
        root = self._verification_store.create_or_update_record(
            self.fs_manager,
            verification_id=verification_id,
            problem_id=int(problem_id),
            workspace_id=None if workspace_id is None else int(workspace_id),
            signature=signature,
            kind=kind,
            status=status,
        )
        if detail is not None:
            self.persist_verification_detail(verification_id, detail)
        return root

    def cancel_verification_if_active(self, verification_id: str, *, reason: str, now_text: str) -> bool:
        return self._verification_store.cancel_active_verification(
            verification_id,
            reason=reason,
            now_text=now_text,
        )

    def update_verification_record_status(
        self,
        verification_id: str,
        *,
        status: str,
        fail_reason: str,
        finished: bool,
    ) -> None:
        self._verification_store.update_record_status(
            verification_id,
            status=status,
            fail_reason=fail_reason,
            finished=finished,
        )

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        _ = values

    def _resolve_standard_checker_source(self, checker_standard: str) -> Path | None:
        return resolve_standard_checker_source(
            checker_standard,
            standard_checker_root=STANDARD_CHECKER_ROOT,
            name_pattern=STANDARD_CHECKER_NAME_RE,
        )

    def _select_checker_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        return select_checker_source(
            snapshot=snapshot,
            build_cfg=build_cfg,
            standard_checker_root=STANDARD_CHECKER_ROOT,
            standard_checker_name_re=STANDARD_CHECKER_NAME_RE,
            cpp_extensions=CPP_EXTENSIONS,
            snapshot_resolved=snapshot_resolved,
        )

    def _select_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        config_key: str,
        folder: str,
        preferred: str | None = None,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        return select_source(
            snapshot=snapshot,
            build_cfg=build_cfg,
            config_key=config_key,
            folder=folder,
            cpp_extensions=CPP_EXTENSIONS,
            preferred=preferred,
            snapshot_resolved=snapshot_resolved,
        )

    def _load_build_config(self, snapshot: Path) -> dict:
        cfg = {
            "generator_runs": 3,
            "require_generator": False,
            "require_validator": True,
            "require_checker": True,
            "compile_jobs": 0,
            "validate_jobs": 0,
            "solve_jobs": 0,
            "run_jobs": 0,
            "run_timeout_sec": 30,
            "generator_args": [],
            "generator_sources": [],
            "validator_args": [],
            "checker_args": [],
            "checker_standard": "",
        }
        path = snapshot / "config" / "build.json"
        if path.exists():
            try:
                cfg.update(dict(json.loads(path.read_text(encoding="utf-8"))))
            except json.JSONDecodeError:
                pass
        cfg["checker_standard"] = cfg["checker_standard"].strip()
        try:
            cfg["compile_jobs"] = max(0, min(16, int(cfg.get("compile_jobs", 0))))
        except Exception:
            cfg["compile_jobs"] = 0
        try:
            cfg["validate_jobs"] = max(0, min(16, int(cfg.get("validate_jobs", 0))))
        except Exception:
            cfg["validate_jobs"] = 0
        try:
            cfg["solve_jobs"] = max(0, min(16, int(cfg.get("solve_jobs", 0))))
        except Exception:
            cfg["solve_jobs"] = 0
        try:
            cfg["run_jobs"] = max(0, min(16, int(cfg.get("run_jobs", 0))))
        except Exception:
            cfg["run_jobs"] = 0
        try:
            cfg["run_timeout_sec"] = max(1, min(300, int(cfg.get("run_timeout_sec", 30))))
        except Exception:
            cfg["run_timeout_sec"] = 30
        return cfg

    def _load_problem_runtime_config(self, snapshot: Path) -> dict:
        return load_problem_runtime_config(
            snapshot,
            default_time_limit_ms=DEFAULT_TIME_LIMIT_MS,
            default_mode="pass-fail",
            min_time_limit_ms=TIME_LIMIT_MIN_MS,
            max_time_limit_ms=TIME_LIMIT_MAX_MS,
        )

    def _manual_test_sources(self, snapshot: Path) -> list[Path]:
        return manual_test_sources(snapshot)

    def _load_tests_spec(self, snapshot: Path) -> list[dict] | None:
        return load_tests_spec_entries(snapshot)

    def _prepare_tests_spec_runtime(
        self,
        snapshot: Path,
        tests_spec_entries: list[dict],
        bin_dir: Path,
    ) -> tuple[list[dict], list[tuple[str, Path, Path]]]:
        return prepare_tests_spec_runtime(
            snapshot,
            tests_spec_entries,
            bin_dir,
            generator_source_extensions=GENERATOR_SOURCE_EXTENSIONS,
            parse_gen_command_tokens_fn=parse_gen_command_tokens,
        )

    def run_verification(
        self,
        problem: str,
        username: str,
        commit: str | None = None,
        ref: str | None = None,
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
        signature_root = workspace_path
        if commit:
            resolved_commit = self.workspace_service.resolve_commit(workspace_path, commit)
            snapshot_root = self.workspace_service.create_snapshot(workspace_path, resolved_commit)
            workspace_dirty = False
            signature_root = snapshot_root
        elif workspace_dirty or (not workspace_head):
            snapshot_root = self.workspace_service.create_snapshot(
                workspace_path,
                None,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
            )
            signature_root = snapshot_root
        signature = verification_signature(signature_root)
        target_verification_id = verification_id or self._verification_store.allocate_id()
        run_workspace_verification_dag(
            problem,
            username,
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
            targets=[],
            verification_id=target_verification_id,
            signature=signature,
            kind=Kind.SAMPLE.value if sample_only else Kind.ALL.value,
            sample_only=bool(sample_only),
            snapshot_root_override=snapshot_root,
        )
        return target_verification_id
