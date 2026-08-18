from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_execute,
    db_fetch_all,
    db_fetch_one,
    db_write_transaction,
    verification_programs_for_tasks,
)

import asyncio
import io
import re
import shutil
import threading
import zipfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from fastapi import HTTPException

from app.config import CONFIG_REGISTRY
from app.service.problem.test_spec import normalize_file_manual_input, normalize_manual_input
from app.service.platform.git_process import GitCommandResult, run_git
from app.service.repository.revision import workspace_revision_info
from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    verification_task_id,
)
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus
from app.service.statement.constant import (
    DEFAULT_OLYMP_STY,
    DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    DEFAULT_STATEMENT_TEMPLATE,
)
from app.service.statement.render import (
    ensure_statement_language_sources,
    statement_templates_are_default,
)
from app.impl.problem.merge_op import merge_apply, merge_compare, merge_page
from app.impl.root.contests import import_package_as_new_problem
from tests.package_builders import polygon_contest_package, polygon_problem_package
from tests.common import E2ETestBase
from tests.identity_helpers import canonical_test_verification_id

from tests.ui_support import (
    AUTH_COOKIE_NAME,
    Path,
    UIHelpersMixin,
    _cookie_value_from_response,
    _flash_messages_from_response,
    _post_form_request,
    _post_request,
    _register_with_password_envelope,
    _request,
    _request_with_cookie,
    _sudo_with_password_envelope,
    access_page,
    runtime,
    contests_root_create,
    contests_root_import,
    contests_root_import_confirm,
    contests_root_import_review,
    contests_root_page,
    files_page,
    general_page,
    general_save,
    git_discard_path,
    git_service,
    history_import,
    history_page,
    history_snapshot,
    json,
    problem_delete,
    problems_root_import,
    problems_root_import_slug_hint,
    problems_root_page,
    preview_page,
    revision_commit,
    statement_templates_reset,
    statement_examples_template_save,
    statement_examples_template_toggle,
    switch_workspace,
    uuid,
    workspace_access_grant,
    workspace_access_revoke,
    workspace_delete,
    workspace_page,
    workspace_service,
)

TEXTAREA_MAX_BYTES = int(CONFIG_REGISTRY.defaults()["TEXTAREA_MAX_BYTES"])

SUDO_COOKIE_NAME = runtime.config_values.SUDO_COOKIE_NAME


class TestUIWorkspace(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = False
    seed_default_workspace = True

    def _ensure_committed_head(self, problem: str, user: str) -> tuple[Path, str]:
        ws = Path(workspace_service.ensure_workspace(problem, user))
        head_res = run_git(["git", "-C", str(ws), "rev-parse", "HEAD"])
        head = head_res.stdout.strip() if head_res.returncode == 0 else ""
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return ws, head
        marker_rel = f"notes/ui-seed-{uuid.uuid4().hex[:8]}.txt"
        marker = ws / marker_rel
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("seed\n", encoding="utf-8")
        self.assertEqual(run_git(["git", "-C", str(ws), "config", "user.name", user]).returncode, 0)
        self.assertEqual(run_git(["git", "-C", str(ws), "config", "user.email", f"{user}@polygonlike.local"]).returncode, 0)
        # Seed commit should include the default workspace skeleton to avoid pull conflicts in sibling workspaces.
        self.assertEqual(run_git(["git", "-C", str(ws), "add", "-A"]).returncode, 0)
        commit = run_git(["git", "-C", str(ws), "commit", "-m", f"ui-seed-{uuid.uuid4().hex[:6]}"])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        push = run_git(["git", "-C", str(ws), "push", "origin", "HEAD:main"])
        self.assertEqual(push.returncode, 0, push.stderr or push.stdout)
        workspace_service.ensure_workspace(problem, user, refresh_status=True)
        refreshed = run_git(["git", "-C", str(ws), "rev-parse", "HEAD"])
        refreshed_head = refreshed.stdout.strip() if refreshed.returncode == 0 else ""
        self.assertRegex(refreshed_head, r"^[0-9a-f]{40}$")
        return ws, refreshed_head

    def _issue_auth_cookie_header(self, username: str, password: str) -> str:
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        auth_token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(auth_token)
        return f"{AUTH_COOKIE_NAME}={auth_token}"

    def test_system_admin_can_view_and_manage_all_problems(self) -> None:
        problem = f"bob/admin-problem-{uuid.uuid4().hex[:8]}"
        target_user = self.random_id("pread")
        workspace_service.ensure_user(target_user)
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, "bob", "owner")
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        root_page = problems_root_page(_request("/problems"), user="alice")
        self.assertEqual(root_page.status_code, 200)
        root_html = root_page.body.decode("utf-8", errors="replace")
        self.assertIn(problem, root_html)
        self.assertNotIn("Workspace / Published", root_html)
        self.assertIn("revision-pair-values-only", root_html)
        self.assertIn("Workspace revision", root_html)
        admin_access = runtime.access_query.problem_context(
            workspace_service.known_problem_id(problem),
            workspace_service.known_user_id("alice"),
        )
        self.assertEqual(admin_access["role"], "admin")
        self.assertTrue(admin_access["can_manage"])

        admin_page = workspace_page(_request(f"/problems/{problem}/workspace"), problem, "alice")
        self.assertEqual(admin_page.status_code, 200)

        grant = workspace_access_grant(problem, "alice", target_user=target_user, role="read")
        self.assertEqual(grant.status_code, 303)
        acl_row = db_fetch_one(
            """
            SELECT role
            FROM repo_acl
            WHERE problem_id=(SELECT id FROM problems WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE LOWER(username)=LOWER(?))
            """,
            [problem, target_user],
        )
        self.assertIsNotNone(acl_row)
        self.assertEqual(str(acl_row["role"] or ""), "read")

    def test_workspace_delete_requires_sudo_then_deletes_copy(self) -> None:
        username = self.random_id("wsdel")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        workspace_service.grant_repo_access("alice/sample", username, "owner")
        ws = Path(workspace_service.ensure_workspace("alice/sample", username))
        marker = ws / f"notes/delete-{uuid.uuid4().hex[:8]}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("temporary\n", encoding="utf-8")

        denied = workspace_delete(
            request=_request_with_cookie(
                "/problems/alice/sample/workspace/delete",
                auth_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem="alice/sample",
            user=username,
        )
        self.assertEqual(denied.status_code, 303)
        self.assertIn("/sudo?next=", denied.headers.get("location", ""))

        sudo_resp = _sudo_with_password_envelope(auth_cookie, password, next_path="/problems/alice/sample/workspace")
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        deleted = workspace_delete(
            request=_request_with_cookie(
                "/problems/alice/sample/workspace/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem="alice/sample",
            user=username,
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual("/problems", deleted.headers.get("location", ""))
        self.assertFalse(ws.exists())
        ws_row = db_fetch_one(
            """
            SELECT id,path,branch,head_commit,dirty FROM workspaces
            WHERE problem_id=(SELECT id FROM problems WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE username=?)
            """,
            ["alice/sample", username],
        )
        self.assertIsNotNone(ws_row)
        self.assertEqual(str(ws_row["path"]), str(ws))
        self.assertEqual(int(ws_row["dirty"] or 0), 0)

    def test_problem_delete_requires_sudo_and_confirmation(self) -> None:
        username = self.random_id("pdel")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        problem = f"alice/pdel-problem-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, username, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, username))
        self.assertTrue(ws.exists())
        row_before = db_fetch_one("SELECT id,repo_name FROM problems WHERE slug=?", [problem])
        self.assertIsNotNone(row_before)
        bare_repo = Path(runtime.settings.bare_root) / str(row_before["repo_name"])
        self.assertTrue(bare_repo.exists())

        denied = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/problem/delete",
                auth_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem=problem,
        )
        self.assertEqual(denied.status_code, 303)
        self.assertIn("/sudo?next=", denied.headers.get("location", ""))

        sudo_resp = _sudo_with_password_envelope(auth_cookie, password, next_path=f"/problems/{problem}/workspace")
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        mismatch = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/problem/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem="wrong-slug",
        )
        self.assertEqual(mismatch.status_code, 303)
        self.assertIn(f"/problems/{problem}/workspace", mismatch.headers.get("location", ""))
        mismatch_messages = _flash_messages_from_response(mismatch)
        self.assertTrue(any("confirmation mismatch" in item for item in mismatch_messages))
        self.assertIsNotNone(db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem]))

        deleted = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/problem/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem=problem.rsplit("/", 1)[-1],
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual("/problems", deleted.headers.get("location", ""))
        self.assertIsNone(db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem]))
        self.assertFalse(ws.exists())
        self.assertFalse(bare_repo.exists())

    def test_problem_delete_route_accepts_fully_qualified_slug_confirmation(self) -> None:
        username = self.random_id("pdelroute")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        problem = f"{username}/route-problem-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, username, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, username))
        row_before = db_fetch_one("SELECT id,repo_name FROM problems WHERE slug=?", [problem])
        self.assertIsNotNone(row_before)
        bare_repo = Path(runtime.settings.bare_root) / str(row_before["repo_name"])
        self.assertTrue(ws.exists())
        self.assertTrue(bare_repo.exists())
        workspace_row = db_fetch_one(
            "SELECT id FROM workspaces WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username=?)",
            [int(row_before["id"]), username],
        )
        self.assertIsNotNone(workspace_row)
        workspace_id = int(workspace_row["id"])
        verification_id = canonical_test_verification_id(
            f"delete-history:{uuid.uuid4().hex}"
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=int(row_before["id"]),
            workspace_id=workspace_id,
            signature="",
            source_commit="",
            kind="all",
        )
        self.assertEqual(admission.outcome, "admitted")
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        tasks = [
            PlannedTask(
                task_id=task_id,
                predecessor_task_id=None,
                task_kind="main-correct",
                source_path="solutions/a.cpp",
                program_id="accepted",
                test_name="001.in",
                expected_behavior="accepted",
            )
        ]
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
        )
        self.assertEqual(activation.outcome, "activated")
        failure = runtime.verification_service.fail_verification(
            verification_id,
            reason="delete history fixture",
        )
        self.assertEqual(failure.outcome, "transitioned")
        db_execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,verification_id,source_commit,source_ref,status,summary_json,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                f"p-delete-history-{uuid.uuid4().hex[:8]}",
                int(row_before["id"]),
                workspace_id,
                verification_id,
                "",
                "",
                "ok",
                "{}",
                "2026-04-08T00:00:00Z",
                "2026-04-08T00:00:01Z",
            ],
        )

        sudo_resp = _sudo_with_password_envelope(
            auth_cookie,
            password,
            next_path=f"/problems/{problem}/workspace",
        )
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        from app.main import app

        with TestClient(app) as client:
            deleted = client.post(
                f"/problems/{problem}/problem/delete",
                data={"confirm_problem": problem},
                headers={"cookie": both_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual("/problems", deleted.headers.get("location", ""))
        self.assertIsNone(db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem]))
        self.assertFalse(ws.exists())
        self.assertFalse(bare_repo.exists())

    def test_problem_delete_finds_active_job_behind_terminal_history(self) -> None:
        problem = f"alice/delete-history-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace = workspace_service.workspace_context(
            problem,
            "alice",
            include_recent=False,
        )
        problem_id = int(workspace["problem"]["id"])
        workspace_id = int(workspace["workspace"]["id"])
        verification_id = canonical_test_verification_id(
            f"delete-history-active:{self.test_id}"
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
        )
        self.assertEqual(admission.outcome, "admitted")

        def _insert_history(conn) -> None:
            conn.executemany(
                """
                INSERT INTO previews(
                    id,problem_id,workspace_id,verification_id,source_commit,
                    source_ref,status,summary_json,created_at,finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        f"preview-terminal-{index:03}",
                        problem_id,
                        workspace_id,
                        None,
                        "",
                        "",
                        "ok",
                        "{}",
                        f"2099-01-01T00:00:00.{index:03}Z",
                        f"2099-01-01T00:00:01.{index:03}Z",
                    )
                    for index in range(65)
                ],
            )

        db_write_transaction(_insert_history)

        with self.assertRaisesRegex(ValueError, "verification jobs are active"):
            workspace_service.delete_problem(problem)

        self.assertIsNotNone(
            db_fetch_one("SELECT id FROM problems WHERE id=?", [problem_id])
        )

    def test_problem_delete_and_activation_have_one_serial_outcome(self) -> None:
        problem = f"alice/delete-activate-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace = workspace_service.workspace_context(
            problem,
            "alice",
            include_recent=False,
        )
        problem_id = int(workspace["problem"]["id"])
        workspace_id = int(workspace["workspace"]["id"])
        verification_id = canonical_test_verification_id(
            f"delete-activate:{self.test_id}"
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
        )
        self.assertEqual(admission.outcome, "admitted")
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        task = PlannedTask(
            task_id=task_id,
            predecessor_task_id=None,
            task_kind="main-correct",
            source_path="solutions/accepted.cpp",
            program_id="accepted",
            test_name="001.in",
            expected_behavior="accepted",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=verification_programs_for_tasks((task,)),
            tasks=(task,),
        )
        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}
        failures: list[BaseException] = []

        def _activate() -> None:
            try:
                barrier.wait()
                outcomes["activate"] = (
                    runtime.verification_service.activate_verification(plan).outcome
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        def _delete() -> None:
            try:
                barrier.wait()
                workspace_service.delete_problem(problem)
                outcomes["delete"] = "deleted"
            except ValueError:
                outcomes["delete"] = "blocked"
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = (
            threading.Thread(target=_activate),
            threading.Thread(target=_delete),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertIn(
            (outcomes["activate"], outcomes["delete"]),
            {("activated", "blocked"), ("missing", "deleted")},
        )
        current_problem_id = workspace_service.known_problem_id(problem)
        self.assertEqual(
            current_problem_id is None,
            outcomes["delete"] == "deleted",
        )

    def test_problem_delete_rejects_terminal_verification_runtime(self) -> None:
        problem = f"alice/delete-draining-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace = workspace_service.workspace_context(
            problem,
            "alice",
            include_recent=False,
        )
        problem_id = int(workspace["problem"]["id"])
        workspace_id = int(workspace["workspace"]["id"])
        verification_id = canonical_test_verification_id(
            f"delete-draining:{self.test_id}"
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
        )
        self.assertEqual(admission.outcome, "admitted")
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        task = PlannedTask(
            task_id=task_id,
            predecessor_task_id=None,
            task_kind="main-correct",
            source_path="solutions/accepted.cpp",
            program_id="accepted",
            test_name="001.in",
            expected_behavior="accepted",
        )
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks((task,)),
            tasks=(task,),
        )
        self.assertEqual(activation.outcome, "activated")
        self.assertTrue(
            runtime.verification_task_store.bind_and_expose_judgehost_runtime(
                task_id,
                expected_verification_id=verification_id,
                expected_program_id="accepted",
                expected_test_name="001.in",
                run_id=f"run-{self.test_id}",
                judgehost_task_id=f"judgehost-{self.test_id}",
                expose=lambda: None,
            )
        )
        runtime.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id=f"run-{self.test_id}",
                    judgehost_task_id=f"judgehost-{self.test_id}",
                    result=normalize_execution_result(verdict="OK"),
                ),
            )
        )

        try:
            with self.assertRaisesRegex(ValueError, "runtime is draining"):
                workspace_service.delete_problem(problem)
            self.assertIsNotNone(
                workspace_service.known_problem_id(problem)
            )
        finally:
            runtime.verification_task_store.unbind_judgehost_runtime(
                task_id,
                judgehost_task_id=f"judgehost-{self.test_id}",
            )

    def test_problem_delete_commits_before_ordered_runtime_cleanup(self) -> None:
        problem = f"alice/delete-cleanup-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.ensure_workspace(problem, "alice")
        self.assertIsNotNone(workspace_service.known_problem_id(problem))

        with patch.object(
            runtime.judgehost_task_service,
            "forget_domjudge_runs",
            side_effect=RuntimeError("scheduler cleanup failed"),
        ) as forget_scheduler, patch.object(
            runtime.judgehost_task_service,
            "forget_problem_tasks",
        ) as forget_registry:
            with self.assertRaisesRegex(RuntimeError, "scheduler cleanup failed"):
                workspace_service.delete_problem(problem)

        forget_scheduler.assert_called_once_with([])
        forget_registry.assert_not_called()
        self.assertIsNone(workspace_service.known_problem_id(problem))

    def test_problem_delete_unexpected_error_redirects_instead_of_500(self) -> None:
        username = self.random_id("pdelx")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        problem = f"alice/pdelx-problem-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, username, "owner")
        workspace_service.ensure_workspace(problem, username)

        sudo_resp = _sudo_with_password_envelope(
            auth_cookie,
            password,
            next_path=f"/problems/{problem}/workspace",
        )
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        with patch.object(workspace_service, "delete_problem", side_effect=Exception("boom")):
            resp = problem_delete(
                request=_request_with_cookie(
                    f"/problems/{problem}/problem/delete",
                    both_cookie,
                    method="POST",
                    extra_headers=[(b"origin", b"http://testserver")],
                ),
                problem=problem,
                user=username,
                confirm_problem=problem,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{problem}/workspace", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("problem delete failed" in item for item in messages))

    def test_problem_delete_rejects_unsafe_repo_name(self) -> None:
        username = self.random_id("pdelu")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        problem = f"alice/pdelu-problem-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, username, "owner")
        workspace_service.ensure_workspace(problem, username)
        db_execute("UPDATE problems SET repo_name='' WHERE slug=?", [problem])

        sudo_resp = _sudo_with_password_envelope(
            auth_cookie,
            password,
            next_path=f"/problems/{problem}/workspace",
        )
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        resp = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/problem/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem=problem,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{problem}/workspace", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("unsafe" in item.lower() for item in messages))
        self.assertIsNotNone(db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem]))

    def test_general_save_persists_problem_config(self) -> None:
        resp = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="3500",
            memory_limit_mb="768",
            mode="interactive",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/alice/sample/statement", resp.headers.get("location", ""))

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        cfg_path = ws / "config" / "problem.json"
        self.assertTrue(cfg_path.exists())
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("time_limit_ms"), 3500)
        self.assertEqual(payload.get("memory_limit_mb"), 768)
        self.assertNotIn("interactive", payload)
        self.assertEqual(payload.get("mode"), "interactive")
        self.assertFalse((ws / "statement" / "rendered").exists())

    def test_general_save_rejects_limits_outside_configured_bounds(self) -> None:
        resp = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="10",
            memory_limit_mb="99999",
            mode="pass-fail",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/alice/sample/statement", resp.headers.get("location", ""))

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        payload = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("time_limit_ms"), 2000)
        self.assertEqual(payload.get("memory_limit_mb"), 1024)

    def test_general_memory_limit_preserves_one_and_clamps_zero_to_one(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        for requested in ("1", "0"):
            with self.subTest(requested=requested):
                response = general_save(
                    problem="alice/sample",
                    user="alice",
                    time_limit_ms="2000",
                    memory_limit_mb=requested,
                    mode="pass-fail",
                )
                self.assertEqual(response.status_code, 303)
                payload = json.loads(
                    (ws / "config" / "problem.json").read_text(encoding="utf-8")
                )
                self.assertEqual(payload.get("memory_limit_mb"), 1)

    def test_general_save_persists_pass_limit(self) -> None:
        resp = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="2000",
            memory_limit_mb="1024",
            mode="interactive",
            pass_limit="2",
        )
        self.assertEqual(resp.status_code, 303)
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        payload = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("mode"), "interactive")
        self.assertEqual(payload.get("pass_limit"), 2)
        self.assertNotIn("interactive", payload)

    def test_general_save_pass_fail_removes_interactor_source(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        files = [
            "solutions/std.cpp",
            "validators/validator.cpp",
            "checkers/checker.cpp",
            "interactors/interactor.cpp",
        ]
        for rel in files:
            path = ws / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("int main(){return 0;}\n", encoding="utf-8")
        build_path = ws / "config" / "build.json"
        build = json.loads(build_path.read_text(encoding="utf-8"))
        build.update(
            {
                "accepted_solution_source": "solutions/std.cpp",
                "validator_source": "validators/validator.cpp",
                "checker_source": "checkers/checker.cpp",
                "interactor_source": "interactors/interactor.cpp",
            }
        )
        build_path.write_text(json.dumps(build, indent=2) + "\n", encoding="utf-8")

        resp = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="2000",
            memory_limit_mb="1024",
            mode="pass-fail",
            pass_limit="2",
        )

        self.assertEqual(resp.status_code, 303)
        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(build_cfg.get("accepted_solution_source"), "solutions/std.cpp")
        self.assertEqual(build_cfg.get("validator_source"), "validators/validator.cpp")
        self.assertEqual(build_cfg.get("checker_source"), "checkers/checker.cpp")
        self.assertNotIn("interactor_source", build_cfg)

    def test_general_save_interactive_removes_checker_and_stale_sources(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        interactor = ws / "interactors" / "interactor.cpp"
        interactor.parent.mkdir(parents=True, exist_ok=True)
        interactor.write_text("int main(){return 0;}\n", encoding="utf-8")
        build_path = ws / "config" / "build.json"
        build = json.loads(build_path.read_text(encoding="utf-8"))
        build.update(
            {
                "accepted_solution_source": "solutions/missing.cpp",
                "validator_source": "validators/missing.cpp",
                "checker_source": "checkers/checker.cpp",
                "interactor_source": "interactors/interactor.cpp",
            }
        )
        build_path.write_text(json.dumps(build, indent=2) + "\n", encoding="utf-8")

        resp = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="2000",
            memory_limit_mb="1024",
            mode="interactive",
            pass_limit="2",
        )

        self.assertEqual(resp.status_code, 303)
        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(build_cfg.get("interactor_source"), "interactors/interactor.cpp")
        self.assertNotIn("checker_source", build_cfg)
        self.assertNotIn("validator_source", build_cfg)
        self.assertNotIn("accepted_solution_source", build_cfg)

    def test_workspace_normalizes_legacy_build_and_reports_review_warning(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        build_path = ws / "config" / "build.json"
        build_path.write_text(
            json.dumps(
                {
                    "accepted_solution_source": "solutions/std.cpp",
                    "checker_args": ["--removed"],
                    "compile_jobs": 0,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        response = workspace_page(
            _request("/problems/alice/sample/workspace"),
            "alice/sample",
            "alice",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(build_path.read_text(encoding="utf-8")),
            {"accepted_solution_source": "solutions/std.cpp"},
        )
        body = response.body.decode("utf-8", errors="replace")
        self.assertIn("obsolete fields were removed", body)

    def test_workspace_keeps_malformed_build_editable_without_rewriting_it(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        build_path = ws / "config" / "build.json"
        malformed = '{"checker_source":"checkers/checker.py"}\n'
        build_path.write_text(malformed, encoding="utf-8")

        response = workspace_page(
            _request("/problems/alice/sample/workspace"),
            "alice/sample",
            "alice",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(build_path.read_text(encoding="utf-8"), malformed)
        body = response.body.decode("utf-8", errors="replace")
        self.assertIn("source must use one of", body)

    def test_malformed_problem_config_can_be_opened_and_replaced_by_general_save(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        problem_path = ws / "config" / "problem.json"
        problem_path.write_text('{"mode":"pass-fail"}\n', encoding="utf-8")

        page = workspace_page(
            _request("/problems/alice/sample/workspace"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            "missing key",
            page.body.decode("utf-8", errors="replace"),
        )

        saved = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="1500",
            memory_limit_mb="64",
            mode="pass-fail",
            pass_limit="1",
        )
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(
            json.loads(problem_path.read_text(encoding="utf-8")),
            {
                "time_limit_ms": 1500,
                "memory_limit_mb": 64,
                "mode": "pass-fail",
                "pass_limit": 1,
            },
        )

    def test_workspace_page_get_refreshes_workspace_status_in_db(self) -> None:
        username = self.random_id("wsget")
        workspace_service.grant_repo_access("alice/sample", username, "owner")
        ws = Path(workspace_service.ensure_workspace("alice/sample", username))
        marker = ws / f"notes/get-dirty-{uuid.uuid4().hex[:8]}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("dirty\n", encoding="utf-8")

        ctx = workspace_service.workspace_context("alice/sample", username, include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        sentinel_updated_at = "2026-03-05T00:00:00Z"
        db_execute(
            """
            UPDATE workspaces
            SET branch=?,
                head_commit=?,
                dirty=?,
                revision_local=?,
                revision_upstream=?,
                revision_missing=?,
                revision_highlight=?,
                revision_upstream_higher=?,
                revision_ahead_count=?,
                revision_behind_count=?,
                updated_at=?
            WHERE id=?
            """,
            ["main", "sentinel-head", 0, 999, 998, 1, 1, 1, 997, 996, sentinel_updated_at, workspace_id],
        )
        before = db_fetch_one(
            "SELECT branch,head_commit,dirty,revision_local,revision_upstream,revision_missing,revision_highlight,revision_upstream_higher,revision_ahead_count,revision_behind_count,updated_at FROM workspaces WHERE id=?",
            [workspace_id],
        )
        self.assertIsNotNone(before)

        resp = workspace_page(_request("/problems/alice/sample/workspace"), "alice/sample", username)
        self.assertEqual(resp.status_code, 200)

        after = db_fetch_one(
            "SELECT branch,head_commit,dirty,revision_local,revision_upstream,revision_missing,revision_highlight,revision_upstream_higher,revision_ahead_count,revision_behind_count,updated_at FROM workspaces WHERE id=?",
            [workspace_id],
        )
        self.assertIsNotNone(after)
        live_status = workspace_service.read_workspace_status(ws)
        live_revision = workspace_revision_info(
            ws,
            str(live_status.get("branch") or "main"),
            workspace_head=str(live_status.get("head_commit") or ""),
            workspace_dirty=bool(live_status.get("dirty")),
        )
        self.assertEqual(str(after["branch"] or ""), str(live_status.get("branch") or ""))
        self.assertEqual(str(after["head_commit"] or ""), str(live_status.get("head_commit") or ""))
        self.assertEqual(int(after["dirty"] or 0), int(live_status.get("dirty") or 0))
        self.assertEqual(after["revision_local"], live_revision["local"])
        self.assertEqual(after["revision_upstream"], live_revision["upstream"])
        self.assertEqual(int(after["revision_missing"] or 0), 1 if live_revision["missing"] else 0)
        self.assertEqual(int(after["revision_highlight"] or 0), 1 if live_revision["highlight"] else 0)
        self.assertEqual(int(after["revision_upstream_higher"] or 0), 1 if live_revision["upstream_higher"] else 0)
        self.assertEqual(after["revision_ahead_count"], live_revision["ahead_count"])
        self.assertEqual(after["revision_behind_count"], live_revision["behind_count"])
        self.assertNotEqual(str(after["updated_at"] or ""), str(before["updated_at"] or ""))

    def test_git_status_and_workspace_status_ignore_hidden_paths(self) -> None:
        username = self.random_id("wshidden")
        workspace_service.grant_repo_access("alice/sample", username, "owner")
        self._ensure_committed_head("alice/sample", username)
        ws = Path(workspace_service.ensure_workspace("alice/sample", username))
        hidden_root = ws / ".env"
        hidden_nested = ws / "notes" / ".cache" / "secret.txt"
        hidden_root.write_text("hidden\n", encoding="utf-8")
        hidden_nested.parent.mkdir(parents=True, exist_ok=True)
        hidden_nested.write_text("nested\n", encoding="utf-8")

        status = git_service.status(ws)
        self.assertNotIn(".env", str(status.get("status") or ""))
        self.assertNotIn(".cache", str(status.get("status") or ""))
        self.assertNotIn(".env", str(status.get("diff") or ""))
        self.assertNotIn(".cache", str(status.get("diff") or ""))

        summary = git_service.status_change_summary(ws, limit=32)
        self.assertEqual(int(summary.get("total") or 0), 0)

        workspace_status = workspace_service.read_workspace_status(ws)
        self.assertEqual(int(workspace_status.get("dirty") or 0), 0)

    def test_git_status_does_not_compute_full_workspace_diff(self) -> None:
        def fake_run_git(args, **kwargs):
            normalized = list(args)
            if normalized[:4] == ["git", "-C", "/tmp/ws", "status"]:
                return GitCommandResult(
                    args=normalized,
                    returncode=0,
                    stdout="## main\n M solutions/std.cpp\n",
                    stderr="",
                    elapsed_ms=1,
                )
            if "diff" in normalized:
                raise AssertionError("workspace status must not compute a full diff")
            return GitCommandResult(args=normalized, returncode=0, stdout="", stderr="", elapsed_ms=1)

        with patch("app.service.repository.git.run_git", side_effect=fake_run_git):
            status = git_service.status(Path("/tmp/ws"))

        self.assertIn("solutions/std.cpp", str(status.get("status") or ""))
        self.assertEqual(status.get("diff"), "")

    def test_workspace_snapshot_copy_excludes_hidden_paths(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        visible = ws / f"notes/snapshot-visible-{uuid.uuid4().hex[:8]}.txt"
        hidden_root = ws / ".env"
        hidden_nested = ws / "notes" / ".cache" / "secret.txt"
        visible.parent.mkdir(parents=True, exist_ok=True)
        visible.write_text("visible\n", encoding="utf-8")
        hidden_root.write_text("hidden\n", encoding="utf-8")
        hidden_nested.parent.mkdir(parents=True, exist_ok=True)
        hidden_nested.write_text("nested\n", encoding="utf-8")

        snapshot = workspace_service.create_snapshot(ws, commit=None, workspace_dirty=True)
        self.assertTrue((snapshot / visible.relative_to(ws)).is_file())
        self.assertFalse((snapshot / ".env").exists())
        self.assertFalse((snapshot / "notes" / ".cache" / "secret.txt").exists())

    def test_files_page_hides_hidden_paths(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        visible = ws / f"notes/files-visible-{uuid.uuid4().hex[:8]}.txt"
        hidden_root = ws / ".env"
        hidden_nested = ws / "notes" / ".cache" / "secret.txt"
        visible.parent.mkdir(parents=True, exist_ok=True)
        visible.write_text("visible\n", encoding="utf-8")
        hidden_root.write_text("hidden\n", encoding="utf-8")
        hidden_nested.parent.mkdir(parents=True, exist_ok=True)
        hidden_nested.write_text("nested\n", encoding="utf-8")

        resp = files_page(_request("/problems/alice/sample/files"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(visible.name, html)
        self.assertNotIn(".env", html)
        self.assertNotIn(".cache", html)

    def test_workspace_review_shows_verification_failure_reason(self) -> None:
        ctx = workspace_service.workspace_context(
            "alice/sample",
            "alice",
            include_recent=False,
        )
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = canonical_test_verification_id(
            f"workspace-review:{uuid.uuid4().hex}"
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature="",
            source_commit=str(ctx["workspace"]["head_commit"] or ""),
            kind="all",
        )
        self.assertEqual(admission.outcome, "admitted")
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        tasks = [
            PlannedTask(
                task_id=task_id,
                predecessor_task_id=None,
                task_kind="main-correct",
                source_path="solutions/accepted.cpp",
                program_id="accepted",
                test_name="001.in",
                expected_behavior="accepted",
            )
        ]
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
        )
        self.assertEqual(activation.outcome, "activated")
        failure = runtime.verification_service.fail_verification(
            verification_id,
            reason="checker exited with code 1",
        )
        self.assertEqual(failure.outcome, "transitioned")

        resp = workspace_page(_request("/problems/alice/sample/workspace"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("checker exited with code 1", html)

    def test_git_discard_path_restores_tracked_file_to_head(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/discard-tracked-{uuid.uuid4().hex[:8]}.txt"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("base\n", encoding="utf-8")
        git_service.commit(ws, f"discard-tracked-base-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        target.write_text("local change\n", encoding="utf-8")

        resp = git_discard_path(problem="alice/sample", user="alice", path=rel)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/alice/sample/workspace", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("discarded file changes", messages[0])
        self.assertEqual(target.read_text(encoding="utf-8"), "base\n")
        status_rows = git_service.status_change_summary(ws, limit=32)["rows"]
        self.assertFalse(any(str(row.get("link_path") or "") == rel for row in status_rows))

    def test_git_discard_path_removes_untracked_file(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/discard-untracked-{uuid.uuid4().hex[:8]}.txt"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("temp\n", encoding="utf-8")

        resp = git_discard_path(problem="alice/sample", user="alice", path=rel)
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(target.exists())
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("discarded file changes", messages[0])

    def test_commit_and_publish_rolls_back_commit_when_push_fails(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/ui-atomic-commit-{uuid.uuid4().hex[:8]}.txt"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("atomic-check\n", encoding="utf-8")
        head_before = run_git(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(head_before)

        with patch.object(git_service, "push", side_effect=RuntimeError("non-fast-forward")):
            resp = revision_commit(problem="alice/sample", user="alice", message=f"ui-atomic-{uuid.uuid4().hex[:6]}")
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/workspace", loc)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("newer published revision", messages[0])

        head_after = run_git(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head_after, head_before)
        status_text = run_git(["git", "-C", str(ws), "status", "--short", "--untracked-files=all"]).stdout
        self.assertIn(rel, status_text)

    def test_clean_workspace_auto_updates_once_when_shared_version_advances(self) -> None:
        alice_ws, _head = self._ensure_committed_head("alice/sample", "alice")
        initial = general_page(_request("/problems/alice/sample/general"), "alice/sample", "alice")
        self.assertEqual(initial.status_code, 200)
        initial_html = initial.body.decode("utf-8", errors="replace")
        self.assertNotIn("/problems/alice/sample/merge/start", initial_html)

        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        bob_ws = Path(workspace_service.ensure_workspace("alice/sample", "bob"))
        self.assertEqual(run_git(["git", "config", "user.name", "Bob"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "config", "user.email", "bob@example.com"], cwd=bob_ws).returncode, 0)
        # Keep this test deterministic when bob workspace already exists from earlier cases.
        self.assertEqual(run_git(["git", "fetch", "origin", "main"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "reset", "--hard", "origin/main"], cwd=bob_ws).returncode, 0)
        marker = f"upstream-{uuid.uuid4().hex[:8]}.txt"
        (bob_ws / marker).write_text("upstream update\n", encoding="utf-8")
        self.assertEqual(run_git(["git", "add", marker], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "commit", "-m", "upstream update"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "push", "origin", "main"], cwd=bob_ws).returncode, 0)

        refreshed = general_page(_request("/problems/alice/sample/general"), "alice/sample", "alice")
        self.assertEqual(refreshed.status_code, 200)
        refreshed_html = refreshed.body.decode("utf-8", errors="replace")
        self.assertNotIn("/problems/alice/sample/merge/start", refreshed_html)
        self.assertIn("Workspace updated to the published revision.", refreshed_html)
        self.assertEqual((alice_ws / marker).read_text(encoding="utf-8"), "upstream update\n")
        repeated = general_page(_request("/problems/alice/sample/general"), "alice/sample", "alice")
        repeated_html = repeated.body.decode("utf-8", errors="replace")
        self.assertNotIn("Workspace updated to the published revision.", repeated_html)

    def test_dirty_workspace_requires_review_when_shared_version_advances(self) -> None:
        alice_ws, old_head = self._ensure_committed_head("alice/sample", "alice")
        local_marker = f"notes/local-{uuid.uuid4().hex[:8]}.txt"
        (alice_ws / local_marker).write_text("local edit\n", encoding="utf-8")

        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        bob_ws = Path(workspace_service.ensure_workspace("alice/sample", "bob"))
        self.assertEqual(run_git(["git", "fetch", "origin", "main"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "reset", "--hard", "origin/main"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "config", "user.name", "Bob"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "config", "user.email", "bob@example.com"], cwd=bob_ws).returncode, 0)
        shared_marker = f"notes/shared-{uuid.uuid4().hex[:8]}.txt"
        (bob_ws / shared_marker).write_text("shared edit\n", encoding="utf-8")
        self.assertEqual(run_git(["git", "add", shared_marker], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "commit", "-m", "shared update"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "push", "origin", "main"], cwd=bob_ws).returncode, 0)

        response = general_page(_request("/problems/alice/sample/general"), "alice/sample", "alice")
        html = response.body.decode("utf-8", errors="replace")
        self.assertIn("/problems/alice/sample/merge/start", html)
        self.assertNotIn("Workspace updated to the published revision.", html)
        self.assertEqual(run_git(["git", "rev-parse", "HEAD"], cwd=alice_ws).stdout.strip(), old_head)
        self.assertTrue((alice_ws / local_marker).is_file())
        self.assertFalse((alice_ws / shared_marker).exists())

        preview = runtime.workspace_merge_service.start_preview("alice", "alice/sample", alice_ws)
        self.assertTrue(preview.suggested_available)

    def test_merge_review_expands_diffs_and_applies_manual_selection(self) -> None:
        alice_ws, _head = self._ensure_committed_head("alice/sample", "alice")
        conflict_path = f"notes/conflict-{uuid.uuid4().hex[:8]}.txt"
        (alice_ws / conflict_path).write_text("mine <script>alert(1)</script>\n", encoding="utf-8")

        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        bob_ws = Path(workspace_service.ensure_workspace("alice/sample", "bob"))
        self.assertEqual(run_git(["git", "fetch", "origin", "main"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "reset", "--hard", "origin/main"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "config", "user.name", "Bob"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "config", "user.email", "bob@example.com"], cwd=bob_ws).returncode, 0)
        (bob_ws / conflict_path).write_text("theirs & shared\n", encoding="utf-8")
        self.assertEqual(run_git(["git", "add", conflict_path], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "commit", "-m", "conflicting file"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_git(["git", "push", "origin", "main"], cwd=bob_ws).returncode, 0)

        preview = runtime.workspace_merge_service.start_preview(
            "alice",
            "alice/sample",
            alice_ws,
        )
        self.assertFalse(preview.suggested_available)
        entry = next(row for row in preview.entries if row.path == conflict_path)
        group_id = entry.group_id

        page = merge_page(
            _request(f"/problems/alice/sample/merge/{preview.preview_id}"),
            "alice/sample",
            preview.preview_id,
            "alice",
        )
        self.assertEqual(page.status_code, 200)

        comparison = merge_compare(
            _request(
                f"/problems/alice/sample/merge/{preview.preview_id}/compare/{entry.entry_id}",
                "target=published",
            ),
            "alice/sample",
            preview.preview_id,
            entry.entry_id,
            "alice",
        )
        self.assertEqual(comparison.status_code, 200)
        payload = json.loads(comparison.body)
        self.assertEqual(payload["path"], conflict_path)
        self.assertEqual(payload["rows"][0]["operation"], "replace")

        invalid = merge_compare(
            _request(
                f"/problems/alice/sample/merge/{preview.preview_id}/compare/{entry.entry_id}",
                "target=invalid",
            ),
            "alice/sample",
            preview.preview_id,
            entry.entry_id,
            "alice",
        )
        self.assertEqual(invalid.status_code, 400)

        applied = asyncio.run(
            merge_apply(
                _post_form_request(
                    f"/problems/alice/sample/merge/{preview.preview_id}/apply",
                    {
                        "mode": "manual",
                        f"choice_{group_id}": "published",
                    },
                ),
                "alice/sample",
                preview.preview_id,
                "alice",
            )
        )
        self.assertEqual(applied.status_code, 303)
        self.assertEqual((alice_ws / conflict_path).read_text(encoding="utf-8"), "theirs & shared\n")

    def test_problem_page_denies_user_without_acl(self) -> None:
        private_problem = f"alice/ui-private-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(private_problem)
        workspace_service.grant_repo_access(private_problem, "bob", "owner")
        workspace_service.ensure_workspace(private_problem, "bob")
        with self.assertRaises(HTTPException) as denied:
            general_page(_request(f"/problems/{private_problem}/alice/general"), private_problem, "alice")
        self.assertEqual(denied.exception.status_code, 403)

    def test_workspace_owner_can_manage_problem_access(self) -> None:
        register_bob = _register_with_password_envelope("bob", "StrongPass123", next_path="/")
        self.assertEqual(register_bob.status_code, 303)
        grant_resp = workspace_access_grant(
            problem="alice/sample",
            user="alice",
            target_user="bob",
            role="write",
        )
        self.assertEqual(grant_resp.status_code, 303)
        member = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["alice/sample", "bob"],
        )
        self.assertIsNotNone(member)
        self.assertEqual(str(member["role"]), "write")

        page = access_page(_request("/problems/alice/sample/access"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Problem Access", html)
        self.assertIn("Grant / Update", html)
        self.assertIn("bob", html)
        self.assertIn('option value="write"', html)
        self.assertIn('option value="read"', html)
        self.assertNotIn('option value="owner"', html)
        self.assertIn("fixed owner", html)

        revoke_resp = workspace_access_revoke(problem="alice/sample", user="alice", target_user="bob")
        self.assertEqual(revoke_resp.status_code, 303)
        removed = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["alice/sample", "bob"],
        )
        self.assertIsNone(removed)

    def test_workspace_access_grant_requires_registered_user(self) -> None:
        target = f"user-{uuid.uuid4().hex[:8]}"
        row = db_fetch_one("SELECT id FROM users WHERE username=?", [target])
        self.assertIsNone(row)

        grant_resp = workspace_access_grant(
            problem="alice/sample",
            user="alice",
            target_user=target,
            role="read",
        )
        self.assertEqual(grant_resp.status_code, 303)
        loc = grant_resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/access", loc)
        grant_messages = _flash_messages_from_response(grant_resp)
        self.assertTrue(grant_messages)
        self.assertIn("register first", grant_messages[0])
        member = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["alice/sample", target],
        )
        self.assertIsNone(member)

    def test_workspace_access_cannot_transfer_owner_role(self) -> None:
        register_bob = _register_with_password_envelope("bob", "StrongPass123", next_path="/")
        self.assertEqual(register_bob.status_code, 303)
        resp = workspace_access_grant(
            problem="alice/sample",
            user="alice",
            target_user="bob",
            role="owner",
        )
        self.assertEqual(resp.status_code, 303)
        grant_messages = _flash_messages_from_response(resp)
        self.assertTrue(grant_messages)
        self.assertIn("owner access is fixed and cannot be transferred", grant_messages[0])
        member = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["alice/sample", "bob"],
        )
        self.assertIsNone(member)

    def test_workspace_access_cannot_revoke_owner(self) -> None:
        db_execute("DELETE FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?)", ["alice/sample"])
        workspace_service.grant_repo_access("alice/sample", "alice", "owner")
        resp = workspace_access_revoke(problem="alice/sample", user="alice", target_user="alice")
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/access", loc)
        revoke_messages = _flash_messages_from_response(resp)
        self.assertTrue(revoke_messages)
        self.assertIn("owner access is fixed and cannot be transferred", revoke_messages[0])

    def test_switch_workspace_denies_existing_problem_without_acl(self) -> None:
        username = self.random_id("switchdeny")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        db_execute("UPDATE users SET is_system_admin=0 WHERE username=?", [username])
        workspace_service.clear_identity_caches()
        private_problem = f"alice/ui-switch-private-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(private_problem)
        workspace_service.grant_repo_access(private_problem, "bob", "owner")
        resp = switch_workspace(
            _request_with_cookie("/switch-workspace", auth_cookie),
            problem=private_problem,
            page="general",
        )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems", loc)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("do not have access to this problem", messages[0])

    def test_switch_workspace_creates_problem_with_slug_leaf_title(self) -> None:
        username = self.random_id("switchcreate")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        slug = f"ui-switch-create-{uuid.uuid4().hex[:8]}"
        resp = switch_workspace(
            _request_with_cookie("/switch-workspace", auth_cookie),
            problem=slug,
            page="statement",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{username}/{slug}/statement", str(resp.headers.get("location", "")))
        ws = Path(workspace_service.ensure_workspace(f"{username}/{slug}", username))
        self.assertFalse((ws / "statement-sections" / "english").exists())

    def test_switch_workspace_lowercases_problem_owner_for_uppercase_username(self) -> None:
        username = "Qingyu"
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        slug = f"ui-switch-upper-{uuid.uuid4().hex[:8]}"

        resp = switch_workspace(
            _request_with_cookie("/switch-workspace", auth_cookie),
            problem=slug,
            page="statement",
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{username.lower()}/{slug}/statement", str(resp.headers.get("location", "")))
        ws = Path(workspace_service.ensure_workspace(f"{username.lower()}/{slug}", username))
        self.assertTrue(ws.exists())

    def test_switch_workspace_opens_single_accessible_foreign_leaf_match_instead_of_creating_duplicate(self) -> None:
        username = self.random_id("switchleaf")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        leaf = f"ui-leaf-{uuid.uuid4().hex[:8]}"
        foreign_problem = f"alice/{leaf}"
        workspace_service.ensure_problem(foreign_problem)
        workspace_service.grant_repo_access(foreign_problem, username, "read")

        resp = switch_workspace(
            _request_with_cookie("/switch-workspace", auth_cookie),
            problem=leaf,
            page="statement",
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{foreign_problem}/statement", str(resp.headers.get("location", "")))
        self.assertIsNone(workspace_service.known_problem_id(f"{username}/{leaf}"))

    def test_switch_workspace_requires_full_problem_id_when_foreign_leaf_exists(self) -> None:
        username = self.random_id("switchamb")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        db_execute("UPDATE users SET is_system_admin=0 WHERE username=?", [username])
        workspace_service.clear_identity_caches()
        leaf = f"ui-amb-{uuid.uuid4().hex[:8]}"
        foreign_problem = f"alice/{leaf}"
        workspace_service.ensure_problem(foreign_problem)

        resp = switch_workspace(
            _request_with_cookie("/switch-workspace", auth_cookie),
            problem=leaf,
            page="statement",
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("already exists under another owner", messages[0])
        self.assertIsNone(workspace_service.known_problem_id(f"{username}/{leaf}"))

    def test_switch_workspace_allows_explicit_owned_problem_id_even_when_foreign_leaf_exists(self) -> None:
        username = self.random_id("switchexpl")
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        leaf = f"ui-explicit-{uuid.uuid4().hex[:8]}"
        foreign_problem = f"alice/{leaf}"
        owned_problem = f"{username}/{leaf}"
        workspace_service.ensure_problem(foreign_problem)

        resp = switch_workspace(
            _request_with_cookie("/switch-workspace", auth_cookie),
            problem=owned_problem,
            page="statement",
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{owned_problem}/statement", str(resp.headers.get("location", "")))
        self.assertIsNotNone(workspace_service.known_problem_id(owned_problem))

    def test_statement_examples_template_is_opt_in_and_editable(self) -> None:
        problem = f"alice/stmtexamples-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        ensure_statement_language_sources(ws, "english")
        examples_path = ws / "statement/examples.tex"

        self.assertFalse(examples_path.exists())

        enabled = statement_examples_template_toggle(
            problem=problem,
            user="alice",
            enabled=True,
            page="statement",
            language="english",
        )
        self.assertEqual(enabled.status_code, 303)
        self.assertEqual(
            examples_path.read_text(encoding="utf-8"),
            DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
        )

        saved = statement_examples_template_save(
            problem=problem,
            user="alice",
            examples_tex="custom ${problem.name}\r\n",
            page="statement",
            language="english",
        )
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(
            examples_path.read_text(encoding="utf-8"),
            "custom ${problem.name}\n",
        )

        disabled = statement_examples_template_toggle(
            problem=problem,
            user="alice",
            enabled=False,
            page="statement",
            language="english",
        )
        self.assertEqual(disabled.status_code, 303)
        self.assertFalse(examples_path.exists())

    def test_fresh_problem_does_not_offer_template_restore(self) -> None:
        problem = f"alice/stmtfresh-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, "alice", "owner")
        workspace_service.ensure_workspace(problem, "alice")

        page = preview_page(
            _request(f"/problems/{problem}/statement"),
            problem,
            "alice",
        )

        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Restore default templates", html)
        self.assertIn('id="statement-language-select"', html)
        self.assertIn('aria-label="Statement language" disabled', html)
        self.assertNotIn("<strong>Language</strong>: missing", html)
        self.assertLess(
            html.index('class="statement-editor-toolbar"'),
            html.index('class="content-section statement-editor-main"'),
        )

    def test_statement_templates_reset_restores_defaults_and_disables_examples_override(self) -> None:
        problem = f"alice/stmtreset-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem)
        workspace_service.grant_repo_access(problem, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        ensure_statement_language_sources(ws, "english")
        (ws / "statement" / "statements.ftl").write_text("custom statements template\n", encoding="utf-8")
        (ws / "statement" / "problem.tex").write_text("custom problem template\n", encoding="utf-8")
        (ws / "statement" / "olymp.sty").write_text("custom olymp style\n", encoding="utf-8")
        examples_path = ws / "statement" / "examples.tex"
        examples_path.write_text("custom examples template\n", encoding="utf-8")
        legend_path = ws / "statement-sections" / "english" / "legend.tex"
        legend_path.write_text("custom legend section\n", encoding="utf-8")

        page = preview_page(_request(f"/problems/{problem}/statement", "language=english"), problem, "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Restore default templates", html)
        self.assertIn(f"/problems/{problem}/statement/templates/reset", html)
        self.assertIn("<strong>Preview:</strong>", html)
        self.assertIn('class="linkish statement-language-add-link"', html)
        self.assertIn('class="inline-action statement-template-reset"', html)
        for label in ("PDF", "HTML", "LaTeX"):
            self.assertRegex(html, rf'<a class="linkish"[^>]*>{label}</a>')
        self.assertIn(">Delete current</a>", html)
        self.assertNotIn("Delete current language</a>", html)
        self.assertLess(html.index(">Delete current</a>"), html.index(">Add language</a>"))

        resp = statement_templates_reset(problem=problem, user="alice", page="statement", language="english")

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(f"/problems/{problem}/statement?language=english", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("default statement templates restored" in item for item in messages))
        self.assertEqual((ws / "statement" / "statements.ftl").read_text(encoding="utf-8"), DEFAULT_STATEMENT_TEMPLATE)
        self.assertEqual((ws / "statement" / "problem.tex").read_text(encoding="utf-8"), DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
        self.assertEqual((ws / "statement" / "olymp.sty").read_text(encoding="utf-8"), DEFAULT_OLYMP_STY)
        self.assertFalse(examples_path.exists())
        self.assertEqual(legend_path.read_text(encoding="utf-8"), "custom legend section\n")

        self.assertTrue(statement_templates_are_default(ws))

    def test_problems_page_shows_only_participating_problems(self) -> None:
        owner_problem = f"alice/ui-owner-{uuid.uuid4().hex[:8]}"
        read_problem = f"alice/ui-read-{uuid.uuid4().hex[:8]}"
        other_problem = f"alice/ui-other-{uuid.uuid4().hex[:8]}"

        workspace_service.ensure_problem(owner_problem)
        workspace_service.ensure_workspace(owner_problem, "alice")
        workspace_service.grant_repo_access(owner_problem, "alice", "owner")

        workspace_service.ensure_problem(read_problem)
        alice_row = db_fetch_one("SELECT id FROM users WHERE username=?", ["alice"])
        read_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [read_problem])
        self.assertIsNotNone(alice_row)
        self.assertIsNotNone(read_row)
        db_execute(
            "INSERT OR IGNORE INTO repo_acl(problem_id,user_id,role,created_at) VALUES(?,?,?,?)",
            [int(read_row["id"]), int(alice_row["id"]), "read", "2026-01-01T00:00:00+00:00"],
        )

        workspace_service.ensure_problem(other_problem)
        workspace_service.ensure_workspace(other_problem, "bob")

        with patch("app.service.platform.git_process.subprocess.run", side_effect=AssertionError("/problems must not run git")):
            resp = problems_root_page(_request("/problems"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(owner_problem, html)
        self.assertIn(read_problem, html)
        self.assertNotIn(other_problem, html)
        self.assertNotIn(f"/problems/{other_problem}/statement", html)

    def test_problems_page_orders_by_last_updated_desc(self) -> None:
        older_slug = f"alice/ui-sort-old-{uuid.uuid4().hex[:8]}"
        newer_slug = f"alice/ui-sort-new-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(older_slug)
        workspace_service.ensure_problem(newer_slug)
        workspace_service.grant_repo_access(older_slug, "alice", "owner")
        workspace_service.grant_repo_access(newer_slug, "alice", "owner")
        workspace_service.ensure_workspace(older_slug, "alice")
        workspace_service.ensure_workspace(newer_slug, "alice")
        db_execute(
            """
            UPDATE workspaces
            SET updated_at=?
            WHERE problem_id=(SELECT id FROM problems WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE username='alice')
            """,
            ["2026-01-01T00:00:00+00:00", older_slug],
        )
        db_execute(
            """
            UPDATE workspaces
            SET updated_at=?
            WHERE problem_id=(SELECT id FROM problems WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE username='alice')
            """,
            ["2026-01-01T00:00:01+00:00", newer_slug],
        )

        resp = problems_root_page(_request("/problems"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(older_slug, html)
        self.assertIn(newer_slug, html)
        self.assertLess(html.find(newer_slug), html.find(older_slug))

    def test_problems_root_import_slug_hint_uses_filename_and_avoids_duplicates(self) -> None:
        token = uuid.uuid4().hex[:8]
        base_slug = f"root-import-hint-{token}"
        workspace_service.ensure_problem(f"alice/{base_slug}")
        resp = problems_root_import_slug_hint(_request("/problems/import/slug-hint"), user="alice", filename=f"{base_slug}.zip", requested_slug="")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertTrue(bool(payload.get("valid")))
        self.assertTrue(bool(payload.get("exists")))
        self.assertEqual(str(payload.get("base") or ""), base_slug)
        suggested = str(payload.get("suggested") or "")
        self.assertTrue(suggested.startswith(base_slug + "-"))
        self.assertNotEqual(suggested, base_slug)

    def test_problems_root_import_slug_hint_strips_polygon_linux_suffix(self) -> None:
        base_slug = f"suffix-trim-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(f"alice/{base_slug}")
        filename = f"{base_slug}-46$linux.zip"
        resp = problems_root_import_slug_hint(_request("/problems/import/slug-hint"), user="alice", filename=filename, requested_slug="")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(str(payload.get("base") or ""), base_slug)
        suggested = str(payload.get("suggested") or "")
        self.assertTrue(suggested.startswith(base_slug + "-"))
        self.assertNotEqual(suggested, base_slug)

    def test_problems_root_import_creates_new_problem(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        upload = _Upload("synthetic-problem.zip", polygon_problem_package())
        target_slug = f"root-import-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"polygon package imported as alice/{target_slug}", messages[0])
        ws = Path(workspace_service.ensure_workspace(f"alice/{target_slug}", "alice"))
        self.assertTrue((ws / "statement" / "statements.ftl").is_file())
        self.assertTrue((ws / "statement-sections" / "english" / "legend.tex").is_file())
        head = run_git(["git", "-C", str(ws), "rev-parse", "HEAD"])
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertRegex(head.stdout.strip(), r"^[0-9a-f]{40}$")
        self.assertEqual(run_git(["git", "-C", str(ws), "status", "--short"]).stdout.strip(), "")

    def test_problems_root_import_recovers_from_stale_user_cache(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        workspace_service.set_cached_user("alice", {"id": 2_147_483_647, "username": "alice"})

        upload = _Upload("synthetic-problem.zip", polygon_problem_package())
        target_slug = f"root-import-cache-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"polygon package imported as alice/{target_slug}", messages[0])

    def test_problems_root_import_accepts_icpc_package(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "icpc/problem.yaml",
                "\n".join(
                    [
                        "problem_format_version: 2025-09",
                        "name: Root Import ICPC",
                        "validation: custom",
                    ]
                )
                + "\n",
            )
            zf.writestr("icpc/data/secret/001.in", "1\n")
            zf.writestr("icpc/data/secret/001.ans", "1\n")
            zf.writestr("icpc/data/sample/1.in", "1\n")
            zf.writestr("icpc/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr("icpc/input_validators/validator.cpp", "int main(){return 0;}\n")
            zf.writestr("icpc/output_validator/checker.cpp", "int main(){return 0;}\n")

        upload = _Upload("root-import-icpc.zip", payload.getvalue())
        target_slug = f"root-icpc-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"icpc package imported as alice/{target_slug}", messages[0])
        ws = Path(workspace_service.ensure_workspace(f"alice/{target_slug}", "alice"))
        self.assertTrue((ws / "tests" / "manual" / "001.in").is_file())
        self.assertFalse((ws / "tests" / "answers").exists())

    def test_problems_root_import_warns_when_english_statement_language_missing(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "poly/problem.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="root-warn-lang">
  <names>
    <name language="english" value="Root Warn Lang"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
""",
            )
            zf.writestr("poly/tests/01", "1\n")
            zf.writestr("poly/tests/01.a", "1\n")
            zf.writestr("poly/statement-sections/russian/legend.tex", "Legend RU\n")

        upload = _Upload("root-import-non-english.zip", payload.getvalue())
        target_slug = f"root-warn-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("warning:", messages[0])
        self.assertIn("english not found", messages[0])
        self.assertIn("defaulting to russian", messages[0])
        ws = Path(workspace_service.ensure_workspace(f"alice/{target_slug}", "alice"))
        self.assertTrue((ws / "statement-sections" / "russian" / "legend.tex").is_file())
        self.assertFalse((ws / "statement-sections" / "english").exists())

    def test_contests_root_page_orders_by_last_updated_desc(self) -> None:
        older_slug = f"ui-contest-sort-old-{uuid.uuid4().hex[:8]}"
        newer_slug = f"ui-contest-sort-new-{uuid.uuid4().hex[:8]}"
        older_create = contests_root_create(_post_request("/contests/create"), user="alice", contest_slug=older_slug, contest_title="Old Contest")
        newer_create = contests_root_create(_post_request("/contests/create"), user="alice", contest_slug=newer_slug, contest_title="New Contest")
        self.assertEqual(older_create.status_code, 303)
        self.assertEqual(newer_create.status_code, 303)
        older_row = db_fetch_one("SELECT id FROM contests WHERE slug=?", [older_slug])
        newer_row = db_fetch_one("SELECT id FROM contests WHERE slug=?", [newer_slug])
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(older_row)
        self.assertIsNotNone(newer_row)
        self.assertIsNotNone(alice_row)
        older_contest_id = int(older_row["id"])
        newer_contest_id = int(newer_row["id"])
        alice_id = int(alice_row["id"])
        db_execute("UPDATE contests SET created_at=? WHERE id=?", ["2026-01-01T00:00:00+00:00", older_contest_id])
        db_execute("UPDATE contests SET created_at=? WHERE id=?", ["2026-01-01T00:00:00+00:00", newer_contest_id])
        db_execute(
            """
            INSERT INTO contest_jobs(
                id,contest_id,actor_user_id,job_type,status,source_generation,created_at,finished_at
            ) VALUES(?,?,?,?,?,1,?,?)
            """,
            ["cj-sort-new", newer_contest_id, alice_id, "build", "ok", "2026-01-01T00:00:05+00:00", "2026-01-01T00:00:05+00:00"],
        )
        db_execute(
            """
            INSERT INTO contest_jobs(
                id,contest_id,actor_user_id,job_type,status,source_generation,created_at,finished_at
            ) VALUES(?,?,?,?,?,1,?,?)
            """,
            ["cj-sort-old", older_contest_id, alice_id, "build", "ok", "2026-01-01T00:00:01+00:00", "2026-01-01T00:00:01+00:00"],
        )

        resp = contests_root_page(_request("/contests"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(older_slug, html)
        self.assertIn(newer_slug, html)
        self.assertLess(html.find(newer_slug), html.find(older_slug))

    def test_contest_import_rejects_more_than_configured_problem_count(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        previous = dict(runtime.config_values.snapshot())
        updated = dict(previous)
        updated["CONTEST_MAX_PROBLEMS"] = 26
        runtime.config_values.replace(updated)
        self.addCleanup(runtime.config_values.replace, previous)
        target_slug = f"contest-too-large-{uuid.uuid4().hex[:8]}"
        response = contests_root_import(
            _post_request("/contests/import"),
            user="alice",
            package_upload=_Upload(
                "too-many.zip",
                polygon_contest_package(problem_count=27),
            ),
            contest_slug=target_slug,
            contest_title="",
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/contests")
        messages = _flash_messages_from_response(response)
        self.assertTrue(messages)
        self.assertIn("configured maximum of 26 problems", messages[0])
        self.assertIsNone(
            db_fetch_one("SELECT id FROM contests WHERE slug=?", [target_slug])
        )

    def test_contests_root_import_polygon_contest_package_creates_contest_and_normalizes_newlines(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        upload = _Upload("synthetic-contest.zip", polygon_contest_package())
        target_slug = f"contest-import-{uuid.uuid4().hex[:8]}"
        custom_problem_slugs = {
            1: f"contest-problem-a-{uuid.uuid4().hex[:8]}",
            2: f"contest-problem-b-{uuid.uuid4().hex[:8]}",
            3: f"contest-problem-c-{uuid.uuid4().hex[:8]}",
            4: f"contest-problem-d-{uuid.uuid4().hex[:8]}",
        }

        resp = contests_root_import(
            _post_request("/contests/import"),
            user="alice",
            package_upload=upload,
            contest_slug=target_slug,
            contest_title="",
        )
        self.assertEqual(resp.status_code, 303)
        location = str(resp.headers.get("location", ""))
        self.assertIn("/contests/import/review?", location)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("contest package parsed (4 problems)", messages[0])

        draft_id = ""
        parsed_location = urlparse(location)
        query = parse_qs(parsed_location.query)
        if "draft_id" in query and query["draft_id"]:
            draft_id = str(query["draft_id"][0] or "").strip()
        self.assertTrue(draft_id)

        review_resp = contests_root_import_review(
            _request("/contests/import/review", f"draft_id={draft_id}"),
            user="alice",
            draft_id=draft_id,
        )
        self.assertEqual(review_resp.status_code, 200)
        review_html = review_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Review Contest Import", review_html)
        self.assertIn('name="problem_slug_1"', review_html)
        self.assertIn('name="problem_slug_4"', review_html)

        confirm_form = {
            "draft_id": draft_id,
            "contest_slug": target_slug,
            "contest_title": "",
            "problem_slug_1": custom_problem_slugs[1],
            "problem_slug_2": custom_problem_slugs[2],
            "problem_slug_3": custom_problem_slugs[3],
            "problem_slug_4": custom_problem_slugs[4],
        }
        confirm_resp = asyncio.run(
            contests_root_import_confirm(
                _post_form_request("/contests/import/confirm", confirm_form),
                user="alice",
            )
        )
        self.assertEqual(confirm_resp.status_code, 303)
        self.assertIn(f"/contests/{target_slug}/overview", str(confirm_resp.headers.get("location", "")))
        confirm_messages = _flash_messages_from_response(confirm_resp)
        self.assertTrue(confirm_messages)
        self.assertIn(f"contest {target_slug} imported (4 problems)", confirm_messages[0])

        contest_row = db_fetch_one("SELECT id,title FROM contests WHERE slug=?", [target_slug])
        self.assertIsNotNone(contest_row)
        self.assertEqual(str(contest_row["title"] or ""), "Synthetic Contest")
        contest_id = int(contest_row["id"])
        default_language_row = db_fetch_one(
            "SELECT statement_default_language FROM contests WHERE id=?",
            [contest_id],
        )
        self.assertIsNotNone(default_language_row)
        self.assertEqual(str(default_language_row["statement_default_language"]), "english")
        attachment_rows = db_fetch_all(
            "SELECT key,rel_path FROM contest_attachments WHERE contest_id=? ORDER BY key ASC",
            [contest_id],
        )
        attachment_keys = [str(row["key"] or "") for row in attachment_rows]
        self.assertIn("statements/english/statements.tex", attachment_keys)
        self.assertIn("statements/english/olymp.sty", attachment_keys)
        contest_source_root = runtime.contest_service.contest_source_root(target_slug)
        self.assertTrue((contest_source_root / "statements" / "english" / "statements.tex").is_file())
        self.assertTrue((contest_source_root / "statements" / "english" / "olymp.sty").is_file())
        imported_rows = runtime.contest_service.contest_problems(contest_id)
        self.assertEqual(len(imported_rows), 4)
        self.assertEqual([str(row["idx"] or "") for row in imported_rows], ["A", "B", "C", "D"])
        statement_folders = [str(row["statement_folder"] or "") for row in imported_rows]
        self.assertTrue(all(statement_folders))
        self.assertEqual(len(set(statement_folders)), 4)
        self.assertEqual(
            [str(row["problem_slug"] or "").strip() for row in imported_rows],
            [
                f"alice/{custom_problem_slugs[1]}",
                f"alice/{custom_problem_slugs[2]}",
                f"alice/{custom_problem_slugs[3]}",
                f"alice/{custom_problem_slugs[4]}",
            ],
        )
        taxi_problem_slug = ""
        for row in imported_rows:
            idx = str(row["idx"] or "").strip().upper()
            if idx == "C":
                taxi_problem_slug = str(row["problem_slug"] or "").strip()
                break
        self.assertTrue(taxi_problem_slug)
        ws = Path(workspace_service.ensure_workspace(taxi_problem_slug, "alice"))
        manual_files = sorted((ws / "tests" / "manual").glob("*.in"))
        self.assertTrue(manual_files)
        self.assertNotIn(b"\r\n", manual_files[0].read_bytes())

        for row in imported_rows:
            problem_slug = str(row["problem_slug"] or "").strip()
            if not problem_slug:
                continue
            pws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
            self.assertFalse((pws / "README.problem.md").exists())
            manual_rows = sorted((pws / "tests" / "manual").glob("*.in"))
            if manual_rows:
                self.assertNotIn(b"\r\n", manual_rows[0].read_bytes())
            self.assertFalse((pws / "tests" / "answers").exists())

    def test_file_manual_input_allows_payloads_larger_than_ui_limit(self) -> None:
        oversized = ("1" * (TEXTAREA_MAX_BYTES + 32)) + "\n"
        with self.assertRaisesRegex(ValueError, "manual test input is too long"):
            normalize_manual_input(oversized, max_bytes=TEXTAREA_MAX_BYTES)
        normalized = normalize_file_manual_input(oversized)
        self.assertGreater(len(normalized.encode("utf-8")), TEXTAREA_MAX_BYTES)
        self.assertTrue(normalized.endswith("\n"))

    def test_contest_import_confirm_rolls_back_partial_contest_on_problem_import_failure(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        upload = _Upload("synthetic-contest.zip", polygon_contest_package())
        target_slug = f"contest-import-{uuid.uuid4().hex[:8]}"

        resp = contests_root_import(
            _post_request("/contests/import"),
            user="alice",
            package_upload=upload,
            contest_slug=target_slug,
            contest_title="",
        )
        draft_id = str(parse_qs(urlparse(str(resp.headers.get("location", ""))).query)["draft_id"][0])
        confirm_form = {
            "draft_id": draft_id,
            "contest_slug": target_slug,
            "contest_title": "",
            "problem_slug_1": f"contest-problem-a-{uuid.uuid4().hex[:8]}",
            "problem_slug_2": f"contest-problem-b-{uuid.uuid4().hex[:8]}",
            "problem_slug_3": f"contest-problem-c-{uuid.uuid4().hex[:8]}",
            "problem_slug_4": f"contest-problem-d-{uuid.uuid4().hex[:8]}",
        }

        real_import = import_package_as_new_problem
        call_count = {"count": 0}
        created_problem_slugs: list[str] = []

        def _failing_import(**kwargs: object) -> dict[str, object]:
            call_count["count"] += 1
            if call_count["count"] == 3:
                raise ValueError("synthetic contest import failure")
            imported = real_import(**kwargs)
            created_problem_slugs.append(str(imported["target_problem"]))
            return imported

        with patch("app.impl.root.contests.import_package_as_new_problem", side_effect=_failing_import):
            confirm_resp = asyncio.run(
                contests_root_import_confirm(
                    _post_form_request("/contests/import/confirm", confirm_form),
                    user="alice",
                )
            )
        self.assertEqual(confirm_resp.status_code, 303)
        self.assertEqual(str(confirm_resp.headers.get("location", "")), "/contests")
        messages = _flash_messages_from_response(confirm_resp)
        self.assertTrue(messages)
        self.assertIn("synthetic contest import failure", messages[0])
        self.assertIsNone(db_fetch_one("SELECT id FROM contests WHERE slug=?", [target_slug]))
        for problem_slug in created_problem_slugs:
            self.assertIsNone(db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug]))

    def test_git_commit_does_not_stage_hidden_paths(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        visible_rel = f"notes/ui-visible-{uuid.uuid4().hex[:8]}.txt"
        visible = ws / visible_rel
        hidden = ws / ".env"
        visible.parent.mkdir(parents=True, exist_ok=True)
        visible.write_text("visible\n", encoding="utf-8")
        hidden.write_text("hidden\n", encoding="utf-8")

        head = git_service.commit(ws, f"ui-hidden-commit-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        shown = run_git(["git", "-C", str(ws), "show", "--name-only", "--pretty=format:", head])
        self.assertEqual(shown.returncode, 0, shown.stderr or shown.stdout)
        names = {line.strip() for line in shown.stdout.splitlines() if line.strip()}
        self.assertIn(visible_rel, names)
        self.assertNotIn(".env", names)

    def test_revision_commit_publishes_first_revision_from_v0(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        marker = f"notes/first-revision-{uuid.uuid4().hex[:8]}.txt"
        target = ws / marker
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("first\n", encoding="utf-8")
        response = revision_commit(
            problem="alice/sample",
            user="alice",
            message=f"first-revision-{uuid.uuid4().hex[:6]}",
        )
        self.assertEqual(response.status_code, 303)
        messages = _flash_messages_from_response(response)
        self.assertTrue(messages)
        self.assertIn("revision published", messages[0])
        self.assertEqual(
            run_git(["git", "-C", str(ws), "rev-parse", "HEAD"]).returncode,
            0,
        )

    def test_git_diff_for_revision_filters_hidden_paths(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self.assertEqual(run_git(["git", "-C", str(ws), "config", "user.name", "alice"]).returncode, 0)
        self.assertEqual(run_git(["git", "-C", str(ws), "config", "user.email", "alice@polygonlike.local"]).returncode, 0)
        hidden = ws / ".env"
        hidden.write_text("hidden\n", encoding="utf-8")
        add = run_git(["git", "-C", str(ws), "add", ".env"])
        self.assertEqual(add.returncode, 0, add.stderr or add.stdout)
        commit = run_git(["git", "-C", str(ws), "commit", "-m", f"ui-hidden-revision-{uuid.uuid4().hex[:6]}"])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        head = run_git(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(head)

        diff_text, truncated = git_service.diff_for_revision(ws, head)
        self.assertFalse(truncated)
        self.assertNotIn(".env", diff_text)
        self.assertNotIn("hidden", diff_text)

    def test_revision_history_page_can_view_selected_revision_diff(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/ui-history-diff-{uuid.uuid4().hex[:8]}.txt"
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("before\n", encoding="utf-8")
        git_service.commit(ws, f"ui-history-diff-base-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        p.write_text("before\nafter\n", encoding="utf-8")
        marker = f"ui-history-diff-{uuid.uuid4().hex[:6]}"
        git_service.commit(ws, marker, "alice", "alice@polygonlike.local")
        selected_version = workspace_revision_info(ws, "main")["local"]
        self.assertIsNotNone(selected_version)

        resp = history_page(_request("/problems/alice/sample/history", f"revision=v{selected_version}"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Revision Changes", html)
        self.assertIn(marker, html)
        self.assertIn("workspace-diff-line-add", html)
        self.assertIn("+after", html)

    def test_history_snapshot_downloads_selected_revision(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"solutions/history-download-{uuid.uuid4().hex[:8]}.cpp"
        source = ws / rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("// historical\n", encoding="utf-8")
        git_service.commit(
            ws,
            f"history-download-{uuid.uuid4().hex[:6]}",
            "alice",
            "alice@polygonlike.local",
        )
        selected_version = workspace_revision_info(ws, "main")["local"]
        self.assertIsNotNone(selected_version)
        source.write_text("// working copy\n", encoding="utf-8")

        response = history_snapshot(
            problem="alice/sample",
            user="alice",
            revision=f"v{selected_version}",
        )
        self.assertEqual(response.status_code, 200)
        archive = Path(str(response.path))
        try:
            with zipfile.ZipFile(archive, "r") as package:
                matches = [name for name in package.namelist() if name.endswith("/" + rel)]
                self.assertEqual(len(matches), 1)
                self.assertEqual(package.read(matches[0]), b"// historical\n")
            self.assertIn(
                f"-v{selected_version}-snapshot.zip",
                str(response.headers.get("content-disposition", "")),
            )
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)

    def test_history_snapshot_can_restore_matching_files_without_deleting_others(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        restored_rel = f"solutions/restore-{uuid.uuid4().hex[:8]}.cpp"
        kept_rel = f"solutions/keep-{uuid.uuid4().hex[:8]}.cpp"
        restored = ws / restored_rel
        kept = ws / kept_rel
        restored.parent.mkdir(parents=True, exist_ok=True)
        restored.write_text("// backup\n", encoding="utf-8")
        snapshot_response = history_snapshot(
            problem="alice/sample",
            user="alice",
            revision="",
        )
        archive = Path(str(snapshot_response.path))
        try:
            payload = archive.read_bytes()
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)

        restored.write_text("// changed\n", encoding="utf-8")
        kept.write_text("// keep\n", encoding="utf-8")
        response = history_import(
            problem="alice/sample",
            user="alice",
            package_upload=_Upload("workspace-snapshot.zip", payload),
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            str(response.headers.get("location", "")),
            "/problems/alice/sample/workspace",
        )
        self.assertEqual(restored.read_text(encoding="utf-8"), "// backup\n")
        self.assertEqual(kept.read_text(encoding="utf-8"), "// keep\n")
