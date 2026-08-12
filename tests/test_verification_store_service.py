from __future__ import annotations

import json
from pathlib import Path

from app.service.disk.verification_store import VerificationStore
from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import VerificationAdmission
from app.service.verification.artifact import artifact_virtual_path
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus

from tests.identity_helpers import canonical_test_verification_id
from tests.isolated_db_helpers import isolated_db_fetch_all, isolated_db_fetch_one
from tests.verification_service_fixture import VerificationServiceTestBase


class TestVerificationStoreService(VerificationServiceTestBase):
    def test_visible_scope_includes_problem_level_but_not_other_workspaces(self) -> None:
        self.workspace_service.ensure_user("bob")
        self.workspace_service.grant_repo_access(self.problem, "bob", "owner")
        self.workspace_service.ensure_workspace(
            self.problem,
            "bob",
            refresh_status=False,
        )
        bob_context = self.workspace_service.workspace_context(
            self.problem,
            "bob",
            include_recent=False,
        )
        bob_workspace_id = int(bob_context["workspace"]["id"])
        verification_ids = {
            "owned": canonical_test_verification_id(self.random_id("ver-owned")),
            "published": canonical_test_verification_id(self.random_id("ver-published")),
            "foreign": canonical_test_verification_id(self.random_id("ver-foreign")),
        }
        for scope, workspace_id in (
            ("owned", self.workspace_id),
            ("published", None),
            ("foreign", bob_workspace_id),
        ):
            admission = self.verification_service.admit_verification(
                VerificationAdmission(
                    verification_id=verification_ids[scope],
                    problem_id=self.problem_id,
                    workspace_id=workspace_id,
                    signature=f"signature-{scope}",
                    source_commit=f"commit-{scope}",
                    kind="all",
                )
            )
            self.assertEqual(admission.outcome, "admitted")

        visible_records = self.verification_service.list_visible_verification_rows(
            self.problem_id,
            self.workspace_id,
        )
        self.assertEqual(
            {row["id"] for row in visible_records},
            {verification_ids["owned"], verification_ids["published"]},
        )
        visible_readiness_rows = self.verification_service.visible_verification_rows(
            self.problem_id,
            self.workspace_id,
        )
        self.assertEqual(
            {row["id"] for row in visible_readiness_rows},
            {verification_ids["owned"], verification_ids["published"]},
        )
        owned_rows = self.verification_service.workspace_verification_rows(
            self.problem_id,
            self.workspace_id,
        )
        self.assertEqual([row["id"] for row in owned_rows], [verification_ids["owned"]])

    def test_verification_detail_lives_in_db_without_sidecar_file(self) -> None:
        self.workspace_service.ensure_workspace(self.problem, self.user)
        ctx = self.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = canonical_test_verification_id(self.random_id("ver-detail-db"))
        self._activate_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="sig-db-only",
            kind="all",
            detail={
                "mode": "pass-fail",
                "pass_limit": 2,
                "selected_test_names": ["001.in", "002.in"],
                "source_paths": ["solutions/ac.cpp", "solutions/wa.cpp"],
                "sanity_checks": ["custom_sample_output"],
                "sanity_check_results": [
                    {
                        "name": "custom_sample_output",
                        "status": "failed",
                        "checked_count": 1,
                        "messages": [
                            {
                                "severity": "failed",
                                "test_name": "001.in",
                                "message": "custom sample output failed on 001.in",
                            }
                        ],
                    }
                ],
                "run_config_json": json.dumps({"checker_mode": "testlib", "pass_limit": 2}),
                "tests_meta_rows": [
                    {"index": 1, "kind": "manual", "desc": "manual", "source": "manual_validate.cpp"},
                    {"index": 2, "kind": "gen", "desc": "gen 2", "source": "generators/gen.cpp", "command": "2", "payload_source": "tests/2"},
                ],
            },
        )

        detail = self.verification_service.verification_detail(verification_id)
        self.assertEqual(detail.get("pass_limit"), 2)
        self.assertEqual(detail.get("selected_test_names"), ["001.in", "002.in"])
        self.assertEqual(detail.get("source_paths"), ["solutions/ac.cpp", "solutions/wa.cpp"])
        self.assertEqual(detail.get("sanity_checks"), ["custom_sample_output"])
        sanity_results = detail.get("sanity_check_results")
        self.assertIsInstance(sanity_results, list)
        self.assertEqual((sanity_results[0] or {}).get("status"), "failed")
        messages = (sanity_results[0] or {}).get("messages")
        self.assertIsInstance(messages, list)
        self.assertEqual((messages[0] or {}).get("message"), "custom sample output failed on 001.in")
        self.assertEqual(str(detail.get("run_config_json") or ""), json.dumps({"checker_mode": "testlib", "pass_limit": 2}))
        tests_meta_rows = detail.get("tests_meta_rows")
        self.assertIsInstance(tests_meta_rows, list)
        self.assertEqual(str((tests_meta_rows[1] or {}).get("test_name") or ""), "002.in")
        self.assertFalse(
            (
                self.storage_layout.cache_artifacts_root
                / "verifications"
                / verification_id
                / "metadata.json"
            ).exists()
        )

    def test_verification_detail_partial_tests_meta_uses_index_not_selected_position(self) -> None:
        self.workspace_service.ensure_workspace(self.problem, self.user)
        ctx = self.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = canonical_test_verification_id(
            self.random_id("ver-detail-partial-meta")
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="sig-partial-meta",
            kind="custom",
            detail={
                "selected_test_names": ["002.in"],
                "tests_meta_rows": [
                    {"index": 1, "kind": "manual", "desc": "manual 1"},
                    {"index": 2, "kind": "manual", "desc": "manual 2"},
                    {"index": 3, "kind": "manual", "desc": "manual 3"},
                ],
            },
        )

        detail = self.verification_service.verification_detail(verification_id)
        rows = detail.get("tests_meta_rows")
        self.assertIsInstance(rows, list)
        self.assertEqual([str(row.get("test_name") or "") for row in rows], ["001.in", "002.in", "003.in"])
        self.assertEqual(detail.get("selected_test_names"), ["002.in"])

    def test_verification_detail_skips_duplicate_tests_meta_names(self) -> None:
        self.workspace_service.ensure_workspace(self.problem, self.user)
        ctx = self.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = canonical_test_verification_id(
            self.random_id("ver-detail-dup-meta")
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="sig-dup-meta",
            kind="custom",
            detail={
                "selected_test_names": ["001.in"],
                "tests_meta_rows": [
                    {"index": 1, "test_name": "001.in", "kind": "manual", "desc": "manual 1"},
                    {"index": 2, "test_name": "001.in", "kind": "manual", "desc": "manual duplicate"},
                ],
            },
        )

        detail = self.verification_service.verification_detail(verification_id)
        rows = detail.get("tests_meta_rows")
        self.assertIsInstance(rows, list)
        self.assertEqual([str(row.get("test_name") or "") for row in rows], ["001.in"])

    def test_verification_artifact_ownership_lives_in_db_not_metadata(self) -> None:
        self.workspace_service.ensure_workspace(self.problem, self.user)
        ctx = self.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = canonical_test_verification_id(
            self.random_id("ver-artifact-refs-db")
        )
        task_id = self._activate_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
            detail={"status": "running", "selected_test_names": ["001.in"]},
        )
        input_ref = self.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="input",
            file_name="001.in",
            payload=b"1 2 3\n",
        )
        answer_ref = self.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="answer",
            file_name="001.ans",
            payload=b"6\n",
        )
        self.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-backend-fixture",
                    judgehost_task_id="",
                    result=normalize_execution_result(verdict="AC"),
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                ),
            )
        )

        self.assertEqual(
            self.verification_service.verification_artifact_ref(verification_id, "001.in", "input_ref"),
            input_ref,
        )
        self.assertEqual(
            self.verification_service.verification_artifact_ref(verification_id, "001.in", "answer_ref"),
            answer_ref,
        )
        metadata = self.verification_service.verification_detail(verification_id)
        self.assertNotIn("artifact_refs", metadata)
        row = isolated_db_fetch_one(
            self.db,
            """
            SELECT role,artifact_ref
            FROM verification_task_artifacts
            WHERE verification_id=? AND task_id=?
            ORDER BY role
            """,
            [verification_id, task_id],
        )
        self.assertIsNotNone(row)
        rows = isolated_db_fetch_all(
            self.db,
            """
            SELECT role,artifact_ref
            FROM verification_task_artifacts
            WHERE verification_id=? AND task_id=?
            ORDER BY role
            """,
            [verification_id, task_id],
        )
        self.assertEqual(
            [(str(item["role"]), str(item["artifact_ref"])) for item in rows],
            [("accepted-answer", answer_ref), ("generated-input", input_ref)],
        )

    def test_artifact_query_uses_owner_index_and_requires_available_blob(self) -> None:
        verification_id = canonical_test_verification_id(
            self.random_id("ver-artifact-query")
        )
        task_id = self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
            signature="",
            kind="all",
            detail={"status": "running", "selected_test_names": ["001.in"]},
        )
        payload = self.runtime_blob_store.put_bytes(
            f"owned-{self.random_id('payload')}\n".encode("utf-8")
        )
        artifact_ref = str(payload.blob_ref)
        self.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-artifact-query",
                    judgehost_task_id="",
                    result=normalize_execution_result(verdict="AC"),
                    input_ref=artifact_ref,
                ),
            )
        )
        self.db.execute(
            "UPDATE verification_tasks SET result_json='{malformed' WHERE id=?",
            [task_id],
        )

        virtual_path = artifact_virtual_path(artifact_ref)
        resolved = self.verification_service.verification_artifact(
            verification_id,
            virtual_path,
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.payload.path, payload.path)
        self.assertEqual(resolved.filename, "001.in")
        for invalid_path in (
            "../tests/001.in",
            "/" + virtual_path,
            "blob//" + virtual_path.removeprefix("blob/"),
            "blob/not-base64!",
            virtual_path + "=",
            virtual_path + "/client-name.txt",
        ):
            self.assertIsNone(
                self.verification_service.verification_artifact(
                    verification_id,
                    invalid_path,
                )
            )

        payload.path.unlink()
        self.assertIsNone(
            self.verification_service.verification_artifact(
                verification_id,
                virtual_path,
            )
        )
        owner = isolated_db_fetch_one(
            self.db,
            """
            SELECT artifact_ref
            FROM verification_task_artifacts
            WHERE verification_id=? AND task_id=? AND role='generated-input'
            """,
            [verification_id, task_id],
        )
        self.assertIsNotNone(owner)
        self.assertEqual(str(owner["artifact_ref"]), artifact_ref)

    def test_create_verification_record_uses_canonical_verification_root(self) -> None:
        ctx = self.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = canonical_test_verification_id(
            self.random_id("ver-artifact-path")
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
        )

        duplicate = self.verification_service.admit_verification(
            VerificationAdmission(
                verification_id=verification_id,
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                signature="",
                source_commit="",
                kind="all",
            )
        )
        self.assertEqual(duplicate.outcome, "already-exists")

        row = VerificationStore(self.db).record_row(verification_id)
        assert row is not None
        self.assertEqual(
            self.verification_service.artifact_path_for_verification(verification_id),
            str(self.storage_layout.prepare_verification_root(verification_id).resolve()),
        )
        self.assertEqual(
            self.storage_layout.prepare_verification_root(verification_id).resolve(),
            Path(self.verification_service.artifact_path_for_verification(verification_id)).resolve(),
        )
