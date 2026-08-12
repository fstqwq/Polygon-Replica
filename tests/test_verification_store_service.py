from __future__ import annotations

import json
from pathlib import Path

from app.service.disk.verification_store import VerificationStore
from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import VerificationAdmission
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_store import VerificationTaskStore

from tests.identity_helpers import canonical_test_verification_id
from tests.isolated_db_helpers import isolated_db_fetch_one
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
        self.assertFalse((self.fs_manager.cache_artifacts_root / "verifications" / verification_id / "metadata.json").exists())

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

    def test_verification_artifact_refs_live_in_db_not_metadata(self) -> None:
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
                    status=VerificationTaskStore.TASK_DONE,
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
            "SELECT input_ref,answer_ref FROM verification_artifact_refs WHERE verification_id=? AND test_name=?",
            [verification_id, "001.in"],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["input_ref"] or ""), input_ref)
        self.assertEqual(str(row["answer_ref"] or ""), answer_ref)

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
            str(self.fs_manager.prepare_verification_root(verification_id).resolve()),
        )
        self.assertEqual(
            self.fs_manager.prepare_verification_root(verification_id).resolve(),
            Path(self.verification_service.artifact_path_for_verification(verification_id)).resolve(),
        )
