from __future__ import annotations

from .db_helpers import db_execute, db_fetch_all, db_fetch_one

import io
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from .common import SmokeBase
from .ui_support import _flash_messages_from_response, _request
from app.impl.run_export import export as export_page_module
from app.impl.run_export import import_source as export_import_module
from app.impl.run_export.import_source import import_package_as_new_problem
from app.impl.workspace.context_job import _run_export_create_worker
from app.impl.runtime.config import config
from app.service.importing import native as native_import_module
from app.service.platform.git_process import run_git
from app.service.importing.native import NativePackageImportService
from app.service.sandbox.base import ExecResult
from app.service.statement.constant import DEFAULT_STATEMENT_PROBLEM_TEMPLATE
from app.service.statement.tex_compile import TexCompileResult

db = config.db
export_service = config.export_service
workspace_service = config.workspace_service


class TestExport(SmokeBase):
    def _seed_export_tests(self, workspace: Path, token: str) -> list[str]:
        tracked = [
            f"tests/manual/{token}.in",
            "tests/spec.json",
        ]
        (workspace / tracked[0]).write_text("1\n", encoding="utf-8")
        (workspace / tracked[1]).write_text(
            json.dumps({"tests": [{"id": token, "kind": "manual", "sample": True, "sample_output": "1\n"}]}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return tracked

    def _insert_exportable_verification(self, verification_id: str, signature: str) -> None:
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        artifact_root = config.fs_manager.prepare_verification_root(str(verification_id or "").strip()).resolve()
        logs = artifact_root / "logs"
        tests = artifact_root / "tests"
        ans = artifact_root / "ans"
        logs.mkdir(parents=True, exist_ok=True)
        tests.mkdir(parents=True, exist_ok=True)
        ans.mkdir(parents=True, exist_ok=True)
        (artifact_root / "manifest.json").write_text("{}\n", encoding="utf-8")
        for name in ["compile.log", "generate.log", "validate.log", "solve.log", "failure.log", "latex.log", "diagnostics.json"]:
            (logs / name).write_text("", encoding="utf-8")
        (tests / "001.in").write_text("1\n", encoding="utf-8")
        (ans / "001.ans").write_text("1\n", encoding="utf-8")

        db_execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,fail_reason,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                problem_id,
                workspace_id,
                signature,
                "all",
                "ok",
                "",
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )

    def _commit_workspace_paths(self, workspace: Path, paths: list[str], message: str) -> str:
        baseline = [
            "statement/statements.ftl",
            "statement/problem.tex",
            "statement/olymp.sty",
            "statement-sections/english/name.tex",
            "statement-sections/english/legend.tex",
            "statement-sections/english/input.tex",
            "statement-sections/english/output.tex",
            "statement-sections/english/notes.tex",
            "third_party/testlib/testlib.h",
        ]
        add = run_git(["git", "-C", str(workspace), "add", *(baseline + list(paths))])
        self.assertEqual(add.returncode, 0, add.stderr)
        commit = run_git(["git", "-C", str(workspace), "commit", "-m", message])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"])
        self.assertEqual(head.returncode, 0, head.stderr)
        return head.stdout.strip()

    def _build_native_package_bytes(self, files: dict[str, str]) -> bytes:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel, content in files.items():
                zf.writestr(rel, content)
        return payload.getvalue()

    def test_export_service_available(self) -> None:
        ws = self._workspace_path()
        self.assertTrue((ws / "third_party" / "testlib" / "testlib.h").is_file())
        self.assertTrue(callable(export_service.create_export))

    def test_export_rejects_non_icpc_type(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_only_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(ws, [rel, f"{rel}.desc"], f"test export type reject {token}")
        verification_id = f"ver-exp-reject-{token}"
        self._insert_exportable_verification(verification_id, head)
        with self.assertRaisesRegex(ValueError, "unsupported export type"):
            export_service.create_export(self.problem, verification_id, "kattis")

    def test_export_persists_requested_verification_id(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_export_vid_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export verification id {token}",
        )
        verification_id = f"ver-exp-bind-{token}"
        self._insert_exportable_verification(verification_id, head)
        workspace_id = int(workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["id"])
        archive = export_service.create_export(
            self.problem,
            verification_id,
            "icpc",
            workspace_id=workspace_id,
            source_commit=head,
        )
        self.assertTrue(archive.exists())
        row = db_fetch_one(
            "SELECT verification_id FROM exports WHERE source_commit=? ORDER BY created_at DESC LIMIT 1",
            [head],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["verification_id"] or ""), verification_id)

    def test_icpc_export_worker_verifies_revision_and_binds_verification_id(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_export_auto_verify_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export auto verify {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = f"ver-export-auto-{token}"

        def _fake_dag(_problem: str, _user: str, **kwargs) -> None:
            self.assertEqual(str(kwargs["source_commit"]), head)
            self.assertEqual(str(kwargs["workspace_head"]), head)
            self.assertFalse(bool(kwargs["workspace_dirty"]))
            snapshot = Path(str(kwargs["snapshot_root_override"]))
            self.assertTrue((snapshot / rel).is_file())
            config.verification_service.begin_verification_record(
                verification_id=verification_id,
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                signature=str(kwargs["signature"]),
                source_commit=str(kwargs["source_commit"]),
                kind=str(kwargs["kind"]),
                status="ok",
                detail={"sanity_status": "passed", "validation_status": "passed"},
            )
            config.verification_service.update_verification_record_status(
                verification_id,
                status="ok",
                fail_reason="",
                finished=True,
            )

        with patch("app.impl.workspace.context_job.run_workspace_verification_dag", side_effect=_fake_dag):
            _run_export_create_worker(
                self.problem,
                self.user,
                actor_user_id=int(ctx["user"]["id"]),
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                source_commit=head,
                requested_verification_id=verification_id,
                requested_export_type="icpc",
                export_task_id=f"exp-test-{token}",
            )

        export_row = db_fetch_one(
            "SELECT verification_id,source_commit FROM exports WHERE source_commit=? ORDER BY created_at DESC LIMIT 1",
            [head],
        )
        self.assertIsNotNone(export_row)
        self.assertEqual(str(export_row["verification_id"] or ""), verification_id)
        self.assertEqual(str(export_row["source_commit"] or ""), head)
        verification_row = config.verification_service.verification_record(verification_id)
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["source_commit"] or ""), head)

    def test_icpc_export_worker_fails_without_package_when_auto_verification_fails(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_export_auto_fail_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export auto verify fail {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = f"ver-export-fail-{token}"

        def _fake_dag(_problem: str, _user: str, **kwargs) -> None:
            config.verification_service.begin_verification_record(
                verification_id=verification_id,
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                signature=str(kwargs["signature"]),
                source_commit=str(kwargs["source_commit"]),
                kind=str(kwargs["kind"]),
                status="failed",
                detail={"sanity_status": "failed", "validation_status": "failed"},
            )
            config.verification_service.update_verification_record_status(
                verification_id,
                status="failed",
                fail_reason="verification failed",
                finished=True,
            )

        with patch("app.impl.workspace.context_job.run_workspace_verification_dag", side_effect=_fake_dag):
            with self.assertRaisesRegex(ValueError, "verification failed"):
                _run_export_create_worker(
                    self.problem,
                    self.user,
                    actor_user_id=int(ctx["user"]["id"]),
                    problem_id=int(ctx["problem"]["id"]),
                    workspace_id=int(ctx["workspace"]["id"]),
                    source_commit=head,
                    requested_verification_id=verification_id,
                    requested_export_type="icpc",
                    export_task_id=f"exp-test-fail-{token}",
                )

        export_count = int(db_fetch_one("SELECT COUNT(*) AS c FROM exports WHERE source_commit=?", [head])["c"])
        self.assertEqual(export_count, 0)

    def test_icpc_export_worker_allows_auto_verification_warning(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_export_auto_warn_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export auto verify warning {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = f"ver-export-warn-{token}"

        def _fake_dag(_problem: str, _user: str, **kwargs) -> None:
            config.verification_service.begin_verification_record(
                verification_id=verification_id,
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                signature=str(kwargs["signature"]),
                source_commit=str(kwargs["source_commit"]),
                kind=str(kwargs["kind"]),
                status="ok",
                detail={
                    "sanity_status": "warning",
                    "validation_status": "warning",
                    "failed_step": "sanity",
                    "error": "solutions/ac_python.py: accepted solution is close to the time limit.",
                },
            )
            config.verification_service.update_verification_record_status(
                verification_id,
                status="ok",
                fail_reason="",
                finished=True,
            )

        with patch("app.impl.workspace.context_job.run_workspace_verification_dag", side_effect=_fake_dag):
            _run_export_create_worker(
                self.problem,
                self.user,
                actor_user_id=int(ctx["user"]["id"]),
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                source_commit=head,
                requested_verification_id=verification_id,
                requested_export_type="icpc",
                export_task_id=f"exp-test-warn-{token}",
            )

        export_count = int(db_fetch_one("SELECT COUNT(*) AS c FROM exports WHERE source_commit=?", [head])["c"])
        self.assertEqual(export_count, 1)

    def test_export_validation_fallback_requires_explicit_verification_id(self) -> None:
        resolved = export_page_module._resolve_export_verification_id(
            problem_id=1,
            workspace_id=1,
            verification_id="",
            source_commit="abc123def456",
        )
        self.assertEqual(resolved, "")

    def test_build_validation_status_respects_explicit_unknown_metadata(self) -> None:
        status = export_page_module._build_validation_status(
            {
                "status": "ok",
                "details": {
                    "validation_status": "unknown",
                },
            }
        )
        self.assertEqual(status, "validation unknown")

    def test_build_validation_status_prefers_sanity_metadata(self) -> None:
        status = export_page_module._build_validation_status(
            {
                "status": "running",
                "details": {
                    "sanity_status": "failed",
                },
            }
        )
        self.assertEqual(status, "validation failed")

    def test_build_validation_status_treats_sanity_warning_as_passed_when_verification_ok(self) -> None:
        status = export_page_module._build_validation_status(
            {
                "status": "ok",
                "details": {
                    "sanity_status": "warning",
                    "validation_status": "warning",
                    "failed_step": "sanity",
                },
            }
        )
        self.assertEqual(status, "validation passed")

    def test_icpc_export_can_be_imported_as_new_problem(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        files = {
            "accepted": f"solutions/ac_roundtrip_{token}.cpp",
            "validator": f"validators/validator_roundtrip_{token}.cpp",
            "checker": f"checkers/checker_roundtrip_{token}.cpp",
            "build_cfg": "config/build.json",
            "statement_asset": f"statement-assets/diagram_{token}.png",
        }
        (ws / files["accepted"]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{files['accepted']}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / files["validator"]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / files["checker"]).write_text("#include \"testlib.h\"\nint main(){return 42;}\n", encoding="utf-8")
        (ws / files["statement_asset"]).parent.mkdir(parents=True, exist_ok=True)
        (ws / files["statement_asset"]).write_bytes(b"PNG")
        (ws / files["build_cfg"]).write_text(
            "{\n"
            f"  \"validator_source\": \"{files['validator']}\",\n"
            f"  \"checker_source\": \"{files['checker']}\",\n"
            f"  \"accepted_solution_source\": \"{files['accepted']}\"\n"
            "}\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(
            ws,
            [
                files["accepted"],
                f"{files['accepted']}.desc",
                files["validator"],
                files["checker"],
                files["statement_asset"],
                files["build_cfg"],
                *self._seed_export_tests(ws, "001"),
            ],
            f"test export import roundtrip {token}",
        )
        archive = export_service.create_export(
            self.problem,
            "",
            "icpc",
            workspace_id=int(workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["id"]),
            source_commit=head,
        )

        actor_row = db_fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"imp-icpc-{token}"
        imported = import_package_as_new_problem(
            actor_user_id=int(actor_row["id"]),
            actor_user=str(actor_row["username"]),
            package_name=archive.name,
            package_content=archive.read_bytes(),
            requested_slug=target_slug,
            source_problem=self.problem,
        )
        self.assertEqual(str(imported.get("package_format") or ""), "icpc")
        target_problem = str(imported.get("target_problem") or "")
        self.assertEqual(target_problem, f"{self.user}/{target_slug}")

        imported_ws = Path(workspace_service.ensure_workspace(target_problem, self.user))
        self.assertTrue((imported_ws / "tests" / "manual" / "001.in").is_file())
        self.assertEqual((imported_ws / "tests" / "manual" / "001.in").read_text(encoding="utf-8"), "1\n")
        imported_tests = json.loads((imported_ws / "tests" / "spec.json").read_text(encoding="utf-8"))["tests"]
        self.assertEqual(str(imported_tests[0].get("sample_output") or ""), "1\n")
        self.assertFalse((imported_ws / "tests" / "answers").exists())
        self.assertTrue((imported_ws / "statement" / "statements.ftl").is_file())
        self.assertFalse((imported_ws / files["statement_asset"]).exists())
        imported_problem_cfg = json.loads((imported_ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertIn(str(imported_problem_cfg.get("mode") or ""), {"pass-fail", "interactive"})
        self.assertGreaterEqual(int(imported_problem_cfg.get("pass_limit") or 0), 1)
        imported_head = run_git(["git", "-C", str(imported_ws), "rev-parse", "HEAD"])
        self.assertEqual(imported_head.returncode, 0, imported_head.stderr)
        self.assertRegex(imported_head.stdout.strip(), r"^[0-9a-f]{40}$")
        self.assertEqual(run_git(["git", "-C", str(imported_ws), "status", "--short"]).stdout.strip(), "")

    def test_interactive_icpc_export_reimports_with_configured_nested_interactor(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        tracked = [
            f"solutions/ac_roundtrip_interactive_{token}.cpp",
            f"solutions/ac_roundtrip_interactive_{token}.cpp.desc",
            f"solutions/wa_roundtrip_interactive_{token}.cpp",
            f"solutions/wa_roundtrip_interactive_{token}.cpp.desc",
            "config/problem.json",
            "config/build.json",
            f"interactors/interactor/interactor_{token}.cpp",
            *self._seed_export_tests(ws, "001"),
        ]
        (ws / tracked[0]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / tracked[1]).write_text("expected: accepted\n", encoding="utf-8")
        (ws / tracked[2]).write_text("int main(){return 1;}\n", encoding="utf-8")
        (ws / tracked[3]).write_text("expected: wrong_answer\n", encoding="utf-8")
        (ws / tracked[6]).parent.mkdir(parents=True, exist_ok=True)
        (ws / tracked[6]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / tracked[4]).write_text(
            json.dumps(
                {
                    "mode": "interactive",
                    "pass_limit": 2,
                    "time_limit_ms": 2000,
                    "memory_limit_mb": 1024,
                    "input_file": "stdin",
                    "output_file": "stdout",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / tracked[5]).write_text(
            json.dumps(
                {
                    "accepted_solution_source": tracked[0],
                    "interactor_source": tracked[6],
                    "generator_sources": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(ws, tracked, f"test interactive export import roundtrip {token}")
        archive = export_service.create_export(
            self.problem,
            "",
            "icpc",
            workspace_id=int(workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["id"]),
            source_commit=head,
        )
        actor_row = db_fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"imp-icpc-interactive-{token}"
        imported = import_package_as_new_problem(
            actor_user_id=int(actor_row["id"]),
            actor_user=str(actor_row["username"]),
            package_name=archive.name,
            package_content=archive.read_bytes(),
            requested_slug=target_slug,
            source_problem=self.problem,
        )
        imported_ws = Path(workspace_service.ensure_workspace(f"{self.user}/{target_slug}", self.user))
        self.assertEqual(str(imported.get("package_format") or ""), "icpc")
        imported_build_cfg = json.loads((imported_ws / "config" / "build.json").read_text(encoding="utf-8"))
        imported_interactor_source = str(imported_build_cfg.get("interactor_source") or "")
        self.assertTrue(imported_interactor_source)
        self.assertTrue((imported_ws / imported_interactor_source).is_file())
        imported_problem_cfg = json.loads((imported_ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(imported_problem_cfg.get("mode"), "interactive")
        self.assertEqual(imported_problem_cfg.get("pass_limit"), 2)
        self.assertEqual(run_git(["git", "-C", str(imported_ws), "status", "--short"]).stdout.strip(), "")

    def test_icpc_export_uses_domjudge_root_layout_and_separated_samples(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        files = {
            "accepted": f"solutions/ac_icpc_layout_{token}.cpp",
            "validator": f"validators/validator_layout_{token}.cpp",
            "checker": f"checkers/checker_layout_{token}.cpp",
            "asset": f"statement-assets/layout_{token}.png",
            "build": "config/build.json",
        }
        (ws / files["accepted"]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{files['accepted']}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / files["validator"]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / files["checker"]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / files["asset"]).parent.mkdir(parents=True, exist_ok=True)
        (ws / files["asset"]).write_bytes(b"PNG")
        (ws / "tests" / "manual" / "001.in").write_text("sample input\n", encoding="utf-8")
        (ws / "tests" / "manual" / "002.in").write_text("secret input\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "tests": [
                        {
                            "id": "001",
                            "kind": "manual",
                            "sample": True,
                            "sample_output": "sample answer\n",
                        },
                        {"id": "002", "kind": "manual", "sample": False},
                    ]
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / files["build"]).write_text(
            json.dumps(
                {
                    "accepted_solution_source": files["accepted"],
                    "validator_source": files["validator"],
                    "checker_source": files["checker"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(
            ws,
            [
                files["accepted"],
                f"{files['accepted']}.desc",
                files["validator"],
                files["checker"],
                files["asset"],
                files["build"],
                "tests/manual/001.in",
                "tests/manual/002.in",
                "tests/spec.json",
            ],
            f"test icpc root layout {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = f"ver-export-layout-{token}"
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature=head,
            source_commit=head,
            kind="all",
            status="ok",
            detail={"sanity_status": "passed", "validation_status": "passed"},
        )
        answer_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="002.in",
            role="answer",
            file_name="002.ans",
            payload=b"secret answer\n",
        )
        config.verification_service.update_verification_artifact_refs(
            verification_id,
            "002.in",
            {"answer_ref": answer_ref},
        )
        archive = export_service.create_export(
            self.problem,
            verification_id,
            "icpc",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )

        public_slug = Path(self.problem).name
        self.assertTrue(archive.name.startswith(f"{public_slug}-v"))
        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("problem.yaml", names)
            self.assertIn("domjudge-problem.ini", names)
            self.assertFalse(any(name.startswith(f"{public_slug}/") for name in names))
            self.assertFalse(any(name.startswith("input_validators/") for name in names))
            self.assertFalse(any(name.endswith(".tex") for name in names))
            self.assertFalse(any(name.startswith("statement-sections/") for name in names))
            self.assertFalse(any(name.startswith("statement-assets/") for name in names))
            self.assertIn("data/sample/001.in", names)
            self.assertIn("data/sample/001.ans", names)
            self.assertNotIn("data/secret/001.in", names)
            self.assertNotIn("data/secret/001.ans", names)
            self.assertIn("data/secret/002.in", names)
            self.assertIn("data/secret/002.ans", names)
            self.assertEqual(zf.read("data/sample/001.ans").decode("utf-8"), "sample answer\n")
            self.assertEqual(zf.read("data/secret/002.ans").decode("utf-8"), "secret answer\n")
            self.assertIn(f"output_validators/checker/{Path(files['checker']).name}", names)
            self.assertIn("output_validators/checker/testlib.h", names)
            domjudge_ini = zf.read("domjudge-problem.ini").decode("utf-8", errors="replace")
            self.assertIn(f"short-name = {public_slug[:32]}", domjudge_ini)
            self.assertRegex(domjudge_ini, r"(?m)^color = #[0-9a-f]{6}$")
            self.assertIn(f"externalid = {public_slug}", domjudge_ini)
            problem_yaml = zf.read("problem.yaml").decode("utf-8", errors="replace")
            self.assertNotIn("problem_format_version", problem_yaml)
        export_row = db_fetch_one("SELECT id FROM exports WHERE filename=?", [archive.name])
        self.assertIsNotNone(export_row)
        summary = export_page_module._export_archive_summary(self.problem, str(export_row["id"]), archive.name)
        self.assertEqual(int(summary.get("tests_total") or 0), 2)
        self.assertEqual(int(summary.get("solutions_total") or 0), 1)

    def test_icpc_export_requires_answers_for_secret_tests(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_missing_ans_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps({"tests": [{"id": "001", "kind": "manual", "sample": False}]}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", "tests/manual/001.in", "tests/spec.json"],
            f"test icpc missing answer {token}",
        )
        with self.assertRaisesRegex(ValueError, "export missing verification answer artifact"):
            export_service.create_export(
                self.problem,
                "",
                "icpc",
                workspace_id=int(workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["id"]),
                source_commit=head,
            )

    def test_icpc_export_uses_verification_answer_artifacts(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_artifact_ans_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps({"tests": [{"id": "001", "kind": "manual", "sample": False}]}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", "tests/manual/001.in", "tests/spec.json"],
            f"test icpc artifact answer {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = f"ver-export-answer-{token}"
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature=head,
            source_commit=head,
            kind="all",
            status="ok",
            detail={"sanity_status": "passed", "validation_status": "passed"},
        )
        answer_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="answer",
            file_name="001.ans",
            payload=b"artifact answer\n",
        )
        config.verification_service.update_verification_artifact_refs(
            verification_id,
            "001.in",
            {"answer_ref": answer_ref},
        )
        archive = export_service.create_export(
            self.problem,
            verification_id,
            "icpc",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )
        with zipfile.ZipFile(archive, "r") as zf:
            self.assertEqual(zf.read("data/secret/001.ans"), b"artifact answer\n")

    def test_icpc_export_emits_domjudge_reference_metadata(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_domjudge_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        problem_cfg = ws / "config" / "problem.json"
        problem_cfg.write_text(
            json.dumps(
                {
                    "mode": "interactive",
                    "pass_limit": 2,
                    "time_limit_ms": 2000,
                    "memory_limit_mb": 1024,
                    "input_file": "stdin",
                    "output_file": "stdout",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "interactors" / f"interactor_{token}.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        build_cfg = ws / "config" / "build.json"
        build_cfg.write_text(
            json.dumps(
                {
                    "accepted_solution_source": rel,
                    "interactor_source": f"interactors/interactor_{token}.cpp",
                    "generator_sources": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(
            ws,
            [
                rel,
                f"{rel}.desc",
                "config/problem.json",
                "config/build.json",
                f"interactors/interactor_{token}.cpp",
                *self._seed_export_tests(ws, "001"),
            ],
            f"test export domjudge metadata {token}",
        )
        archive = export_service.create_export(
            self.problem,
            "",
            "icpc",
            workspace_id=int(workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["id"]),
            source_commit=head,
        )
        with zipfile.ZipFile(archive, "r") as zf:
            problem_yaml = zf.read("problem.yaml").decode("utf-8", errors="replace")
            domjudge_ini = zf.read("domjudge-problem.ini").decode("utf-8", errors="replace")
            self.assertNotIn("problem_format_version", problem_yaml)
            self.assertIn("validation: custom interactive multi-pass", problem_yaml)
            self.assertIn("memory: 1024", problem_yaml)
            self.assertIn("validation_passes: 2", problem_yaml)
            self.assertNotIn("time_limit:", problem_yaml)
            self.assertNotIn("memory_limit:", problem_yaml)
            self.assertIn("timelimit = 2", domjudge_ini)
            self.assertRegex(domjudge_ini, r"(?m)^color = #[0-9a-f]{6}$")
            self.assertIn(f"externalid = {Path(self.problem).name}", domjudge_ini)
            self.assertIn(f"output_validators/interactor/interactor_{token}.cpp", zf.namelist())
            self.assertIn("output_validators/interactor/testlib.h", zf.namelist())
            self.assertIn("output_validators/interactor/build", zf.namelist())
        self.assertTrue(archive.name.startswith(f"{Path(self.problem).name}-v"))

    def test_native_import_rejects_git_metadata_paths(self) -> None:
        ws = Path(self._workspace_path())
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("config/problem.json", json.dumps({"mode": "pass-fail", "pass_limit": 1}))
            zf.writestr(".git/config", "[filter \"evil\"]\n")
            zf.writestr("tests/spec.json", json.dumps({"tests": []}))

        service = NativePackageImportService()
        with self.assertRaisesRegex(ValueError, r"forbidden hidden path: \.git/config"):
            service.import_package(ws, "native-git-metadata.zip", payload.getvalue())

    def test_native_import_rejects_hidden_workspace_paths(self) -> None:
        service = NativePackageImportService()
        blocked_paths = [".env", "solutions/.hidden/file", ".gitignore"]
        for blocked_path in blocked_paths:
            with self.subTest(blocked_path=blocked_path):
                ws = Path(self._workspace_path())
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("config/problem.json", json.dumps({"mode": "pass-fail", "pass_limit": 1}))
                    zf.writestr(blocked_path, "hidden\n")
                    zf.writestr("tests/spec.json", json.dumps({"tests": []}))

                with self.assertRaisesRegex(ValueError, rf"forbidden hidden path: {re.escape(blocked_path)}"):
                    service.import_package(ws, "native-hidden-path.zip", payload.getvalue())

    def test_native_import_rejects_total_unzipped_repo_payload_too_large(self) -> None:
        ws = Path(self._workspace_path())
        sentinel = ws / "solutions" / "keep.cpp"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("keep\n", encoding="utf-8")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("config/problem.json", json.dumps({"mode": "pass-fail", "pass_limit": 1}))
            zf.writestr("solutions/a.cpp", "1234567890")
            zf.writestr("solutions/b.cpp", "abcdefghij")

        service = NativePackageImportService()
        with patch.object(native_import_module, "ZIP_MAX_EXTRACTED_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "repo payload is too large"):
                service.import_package(ws, "native-too-large.zip", payload.getvalue())

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_native_import_rejects_root_files(self) -> None:
        ws = Path(self._workspace_path())
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("config/problem.json", json.dumps({"mode": "pass-fail", "pass_limit": 1}))
            zf.writestr("README.md", "root file\n")
            zf.writestr("tests/spec.json", json.dumps({"tests": []}))

        service = NativePackageImportService()
        with self.assertRaisesRegex(ValueError, r"forbidden root path: README\.md"):
            service.import_package(ws, "native-root-file.zip", payload.getvalue())

    def test_native_import_rejects_repository_answer_files(self) -> None:
        ws = Path(self._workspace_path())
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("config/problem.json", json.dumps({"mode": "pass-fail", "pass_limit": 1}))
            zf.writestr("tests/spec.json", json.dumps({"tests": []}))
            zf.writestr("tests/answers/001.ans", "1\n")

        service = NativePackageImportService()
        with self.assertRaisesRegex(ValueError, r"repository answer file: tests/answers/001\.ans"):
            service.import_package(ws, "native-answer-file.zip", payload.getvalue())

    def test_native_export_roundtrip_preserves_canonical_repo_state(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        tracked = [
            f"solutions/native_{token}.cpp",
            f"solutions/native_{token}.cpp.desc",
            f"validators/native_{token}.cpp",
            "config/problem.json",
            "config/build.json",
            "tests/manual/001.in",
            "tests/spec.json",
        ]
        (ws / tracked[0]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / tracked[1]).write_text("expected: accepted\n", encoding="utf-8")
        (ws / tracked[2]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / tracked[5]).write_text("1\n", encoding="utf-8")
        (ws / tracked[6]).write_text(
            json.dumps({"tests": [{"id": "001", "kind": "manual", "sample": True, "sample_output": "1\n"}]}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (ws / "config" / "problem.json").write_text(
            json.dumps(
                {
                    "mode": "pass-fail",
                    "pass_limit": 1,
                    "time_limit_ms": 2000,
                    "memory_limit_mb": 1024,
                    "input_file": "stdin",
                    "output_file": "stdout",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "accepted_solution_source": tracked[0],
                    "validator_source": tracked[2],
                    "generator_sources": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(ws, tracked, f"test native export roundtrip {token}")
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        baseline_status = run_git(["git", "-C", str(ws), "status", "--short"]).stdout.strip()
        archive = export_service.create_export(
            self.problem,
            "",
            "native",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )
        self.assertEqual(run_git(["git", "-C", str(ws), "status", "--short"]).stdout.strip(), baseline_status)
        actor_row = db_fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"imp-native-{token}"
        imported = import_package_as_new_problem(
            actor_user_id=int(actor_row["id"]),
            actor_user=str(actor_row["username"]),
            package_name=archive.name,
            package_content=archive.read_bytes(),
            requested_slug=target_slug,
            source_problem=self.problem,
        )
        self.assertEqual(str(imported.get("package_format") or ""), "native")
        self.assertEqual(int(imported.get("total_tests") or 0), 1)
        self.assertEqual(int((((imported.get("result") or {}).get("tests") or {}).get("total") or 0)), 1)
        imported_ws = Path(workspace_service.ensure_workspace(f"{self.user}/{target_slug}", self.user))
        self.assertEqual((imported_ws / tracked[0]).read_text(encoding="utf-8"), "int main(){return 0;}\n")
        self.assertEqual((imported_ws / tracked[5]).read_text(encoding="utf-8"), "1\n")
        self.assertEqual(
            json.loads((imported_ws / "config" / "problem.json").read_text(encoding="utf-8")).get("pass_limit"),
            1,
        )
        imported_head = run_git(["git", "-C", str(imported_ws), "rev-parse", "HEAD"])
        self.assertEqual(imported_head.returncode, 0, imported_head.stderr)
        self.assertRegex(imported_head.stdout.strip(), r"^[0-9a-f]{40}$")
        self.assertEqual(run_git(["git", "-C", str(imported_ws), "status", "--short"]).stdout.strip(), "")

    def test_native_export_uses_committed_revision_even_when_working_tree_is_dirty(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/native_dirty_{token}.cpp"
        desc_rel = f"{rel}.desc"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / desc_rel).write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, desc_rel, *self._seed_export_tests(ws, "001")],
            f"test native export committed revision {token}",
        )
        dirty_source = "int main(){return 7;}\n"
        (ws / rel).write_text(dirty_source, encoding="utf-8")

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        archive = export_service.create_export(
            self.problem,
            "",
            "native",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )

        with zipfile.ZipFile(archive, "r") as zf:
            solution_name = next(name for name in zf.namelist() if name.endswith(f"/{rel}"))
            self.assertEqual(zf.read(solution_name).decode("utf-8", errors="replace"), "int main(){return 0;}\n")

        row = db_fetch_one(
            "SELECT source_commit FROM exports WHERE export_type='native' ORDER BY created_at DESC LIMIT 1"
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["source_commit"] or ""), head)

    def test_download_snapshot_uses_working_tree_without_export_activity(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/snapshot_dirty_{token}.cpp"
        desc_rel = f"{rel}.desc"
        hidden_root = ws / ".env"
        hidden_nested = ws / "solutions" / ".cache" / "secret.txt"
        draft_file = ws / "draft" / "skip.txt"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / desc_rel).write_text("expected: accepted\n", encoding="utf-8")
        self._commit_workspace_paths(
            ws,
            [rel, desc_rel, *self._seed_export_tests(ws, "001")],
            f"test snapshot download {token}",
        )
        dirty_source = "int main(){return 9;}\n"
        (ws / rel).write_text(dirty_source, encoding="utf-8")
        hidden_root.write_text("hidden\n", encoding="utf-8")
        hidden_nested.parent.mkdir(parents=True, exist_ok=True)
        hidden_nested.write_text("hidden nested\n", encoding="utf-8")
        draft_file.parent.mkdir(parents=True, exist_ok=True)
        draft_file.write_text("draft\n", encoding="utf-8")

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        exports_before = int(db_fetch_one("SELECT COUNT(*) AS c FROM exports")["c"])
        audit_before = int(
            db_fetch_one(
                "SELECT COUNT(*) AS c FROM audit_log WHERE problem_id=? AND actor_user_id=? AND action='export.create'",
                [int(ctx["problem"]["id"]), int(ctx["user"]["id"])],
            )["c"]
        )
        response = export_page_module.export_snapshot(self.problem, self.user)
        self.assertEqual(response.status_code, 200)
        archive = Path(response.path)
        self.assertTrue(archive.is_file())

        try:
            with zipfile.ZipFile(archive, "r") as zf:
                names = set(zf.namelist())
                package_root = next(name.split("/", 1)[0] for name in names if name.endswith("/config/problem.json"))
                self.assertIn(f"{package_root}/{rel}", names)
                solution_name = f"{package_root}/{rel}"
                self.assertEqual(zf.read(solution_name).decode("utf-8", errors="replace"), dirty_source)
                self.assertNotIn(f"{package_root}/.env", names)
                self.assertNotIn(f"{package_root}/solutions/.cache/secret.txt", names)
                self.assertNotIn(f"{package_root}/draft/skip.txt", names)
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)

        exports_after = int(db_fetch_one("SELECT COUNT(*) AS c FROM exports")["c"])
        audit_after = int(
            db_fetch_one(
                "SELECT COUNT(*) AS c FROM audit_log WHERE problem_id=? AND actor_user_id=? AND action='export.create'",
                [int(ctx["problem"]["id"]), int(ctx["user"]["id"])],
            )["c"]
        )
        self.assertEqual(exports_after, exports_before)
        self.assertEqual(audit_after, audit_before)

    def test_native_export_route_queues_committed_revision_source(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/native_route_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test native export route {token}",
        )
        with patch.object(export_page_module, "start_export_job", return_value=True) as start_job:
            resp = export_page_module.export_create(self.problem, self.user, verification_id="", export_type="native")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(str(start_job.call_args.kwargs["source_commit"]), head)

    def test_icpc_export_route_allocates_verification_for_committed_revision(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/icpc_route_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test icpc export route {token}",
        )
        with patch.object(export_page_module, "start_export_job", return_value=True) as start_job:
            resp = export_page_module.export_create(
                self.problem,
                self.user,
                verification_id="client-provided-id-is-ignored",
                export_type="icpc",
            )
        self.assertEqual(resp.status_code, 303)
        requested_verification_id = str(start_job.call_args.kwargs["requested_verification_id"])
        self.assertTrue(requested_verification_id.startswith("ver-"))
        self.assertNotEqual(requested_verification_id, "client-provided-id-is-ignored")
        self.assertEqual(str(start_job.call_args.kwargs["source_commit"]), head)
        initial_details = dict(start_job.call_args.kwargs["initial_details"])
        self.assertEqual(str(initial_details["verification_id"]), requested_verification_id)

    def test_native_export_route_requires_committed_revision(self) -> None:
        resp = export_page_module.export_create(self.problem, self.user, verification_id="", export_type="native")
        self.assertEqual(resp.status_code, 303)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("no committed revision" in item for item in messages))

    def test_export_page_labels_native_source_as_committed_revision_and_snapshot_action(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/native_page_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test native export page {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        archive = export_service.create_export(
            self.problem,
            "",
            "native",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )
        self.assertTrue(archive.exists())
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                int(ctx["user"]["id"]),
                int(ctx["problem"]["id"]),
                "export.create",
                json.dumps(
                    {
                        "status": "running",
                        "export_type": "native",
                        "source_commit": head,
                        "verification_id": "",
                        "filename": "",
                        "error": "",
                        "export_task_id": "task-ok",
                    }
                ),
                "2026-04-12T02:09:02Z",
            ],
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                int(ctx["user"]["id"]),
                int(ctx["problem"]["id"]),
                "export.create",
                json.dumps(
                    {
                        "status": "ok",
                        "export_type": "native",
                        "source_commit": head,
                        "verification_id": "",
                        "filename": archive.name,
                        "error": "",
                        "export_task_id": "task-ok",
                    }
                ),
                "2026-04-12T02:09:03Z",
            ],
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                int(ctx["user"]["id"]),
                int(ctx["problem"]["id"]),
                "export.create",
                json.dumps(
                    {
                        "status": "running",
                        "export_type": "native",
                        "source_commit": head,
                        "verification_id": "",
                        "filename": "",
                        "error": "",
                        "export_task_id": "task-running",
                    }
                ),
                "2026-04-12T02:09:04Z",
            ],
        )

        resp = export_page_module.export_page(
            _request(f"/problems/{self.problem}/export"),
            self.problem,
            self.user,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Export Into Package", html)
        self.assertIn("Native (committed revision v", html)
        self.assertIn("Download Snapshot", html)
        self.assertIn(f'/problems/{self.problem}/export/snapshot', html)
        self.assertIn("Import Into Working Copy", html)
        self.assertIn("Activity", html)
        self.assertNotIn("Generation Tasks", html)
        self.assertNotIn("Generated Exports", html)
        self.assertIn(f'/problems/{self.problem}/export/import', html)
        self.assertIn(">running<", html)
        self.assertEqual(html.count(">RUNNING<"), 1)
        self.assertEqual(html.count(">OK<"), 1)
        revision = run_git(["git", "-C", str(ws), "rev-list", "--count", head]).stdout.strip()
        self.assertIn(f">v{revision}<", html)
        self.assertNotIn(f">{head[:8]}<", html)

    def test_export_page_failed_activity_keeps_export_error_over_verification_warning(self) -> None:
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        export_error = "export missing verification answer artifact: 001.ans"
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                int(ctx["user"]["id"]),
                int(ctx["problem"]["id"]),
                "export.create",
                json.dumps(
                    {
                        "status": "failed",
                        "export_type": "icpc",
                        "source_commit": "abc123",
                        "verification_id": "ver-export-warning-hidden",
                        "filename": "",
                        "error": export_error,
                        "export_task_id": "task-failed-answer",
                    }
                ),
                "2026-04-12T02:10:00Z",
            ],
        )

        with patch.object(
            export_page_module,
            "_verification_runtime_progress",
            return_value={
                "detail": "solutions/ac_python.py: accepted solution is close to the time limit.",
                "log_href": "",
            },
        ):
            resp = export_page_module.export_page(
                _request(f"/problems/{self.problem}/export"),
                self.problem,
                self.user,
            )

        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(export_error, html)
        self.assertNotIn("accepted solution is close to the time limit", html)

    def test_import_into_working_copy_overwrites_matching_paths_and_keeps_others(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        kept_rel = f"notes/keep-{token}.txt"
        overwritten_rel = f"solutions/std_{token}.cpp"
        (ws / kept_rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / kept_rel).write_text("keep-local\n", encoding="utf-8")
        (ws / overwritten_rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / overwritten_rel).write_text("int main(){return 0;}\n", encoding="utf-8")

        package_bytes = self._build_native_package_bytes(
            {
                "config/problem.json": json.dumps({"name": f"Imported {token}", "mode": "pass-fail", "pass_limit": 1}),
                "tests/spec.json": json.dumps({"tests": [{"id": "001", "kind": "manual", "sample": False}]}),
                "tests/manual/001.in": "5\n",
                overwritten_rel: "int main(){return 7;}\n",
            }
        )

        class _Upload:
            filename = f"import-into-workspace-{token}.zip"

            def __init__(self, raw: bytes):
                self.file = io.BytesIO(raw)

        resp = export_import_module.export_import(
            self.problem,
            self.user,
            package_upload=_Upload(package_bytes),
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{self.problem}/workspace", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"native package imported into working copy {self.problem}", messages[0])
        self.assertEqual((ws / overwritten_rel).read_text(encoding="utf-8"), "int main(){return 7;}\n")
        self.assertEqual((ws / kept_rel).read_text(encoding="utf-8"), "keep-local\n")
        self.assertEqual((ws / "tests" / "manual" / "001.in").read_text(encoding="utf-8"), "5\n")
        self.assertEqual((ws / "statement-sections" / "english" / "name.tex").read_text(encoding="utf-8"), f"Imported {token}\n")

    def test_import_into_working_copy_route_reports_workspace_message(self) -> None:
        package_bytes = self._build_native_package_bytes(
            {
                "config/problem.json": json.dumps({"name": "Workspace Import", "mode": "pass-fail", "pass_limit": 1}),
                "tests/spec.json": json.dumps({"tests": []}),
            }
        )

        class _Upload:
            filename = "workspace-import.zip"

            def __init__(self, raw: bytes):
                self.file = io.BytesIO(raw)

        resp = export_import_module.export_import(
            self.problem,
            self.user,
            package_upload=_Upload(package_bytes),
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{self.problem}/workspace", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"native package imported into working copy {self.problem}", messages[0])

    def test_icpc_export_uses_committed_snapshot_without_verification(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_no_ver_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export without verification {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        baseline_status = run_git(["git", "-C", str(ws), "status", "--short"]).stdout.strip()
        archive = export_service.create_export(
            self.problem,
            "",
            "icpc",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )
        self.assertTrue(archive.exists())
        self.assertEqual(
            run_git(["git", "-C", str(ws), "status", "--short"]).stdout.strip(),
            baseline_status,
        )

    def test_polygon_import_does_not_run_sample_answer_verification(self) -> None:
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="sample-backfill">
  <names>
    <name language="english" value="Sample Answer Backfill"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
  <assets>
    <checker name="custom">
      <source path="files/checker.cpp" type="cpp.g++17"/>
    </checker>
    <validators>
      <validator name="validator">
        <source path="files/validator.cpp" type="cpp.g++17"/>
      </validator>
    </validators>
    <solutions>
      <solution tag="main">
        <source path="files/solution.cpp" type="cpp.g++17"/>
      </solution>
    </solutions>
  </assets>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", "7\n")
            zf.writestr("files/checker.cpp", "int main(){return 42;}\n")
            zf.writestr("files/validator.cpp", "int main(){return 0;}\n")
            zf.writestr(
                "files/solution.cpp",
                "#include <iostream>\n"
                "int main(){std::ios::sync_with_stdio(false);std::cin.tie(nullptr);"
                "long long x=0; if(!(std::cin>>x)) return 0; std::cout<<x<<\"\\n\"; return 0;}\n",
            )

        actor_row = db_fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"poly-backfill-{uuid.uuid4().hex[:8]}"
        with patch("app.impl.run_export.import_source.config.verification_service.run_verification") as run_verification:
            imported = import_package_as_new_problem(
                actor_user_id=int(actor_row["id"]),
                actor_user=str(actor_row["username"]),
                package_name="sample-backfill.zip",
                package_content=payload.getvalue(),
                requested_slug=target_slug,
                source_problem=self.problem,
            )
        run_verification.assert_not_called()
        target_problem = str(imported.get("target_problem") or "")
        imported_ws = Path(workspace_service.ensure_workspace(target_problem, self.user))
        answer_path = imported_ws / "tests" / "answers" / "001.ans"
        self.assertFalse(answer_path.exists())
        tests_summary = imported.get("result", {}).get("tests", {})
        self.assertNotIn("sample_answers_built", tests_summary)
        self.assertNotIn("sample_answers_missing", tests_summary)
        self.assertNotIn("sample_manual_total", tests_summary)

    def test_import_refuses_target_with_existing_revision_history(self) -> None:
        actor_row = db_fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"stale-import-{uuid.uuid4().hex[:8]}"
        target_problem = f"{self.user}/{target_slug}"
        target_bare = (config.settings.bare_root / f"{target_problem}.git").resolve()
        target_bare.parent.mkdir(parents=True, exist_ok=True)
        init = run_git(["git", "init", "--bare", str(target_bare)])
        self.assertEqual(init.returncode, 0, init.stderr or init.stdout)

        with tempfile.TemporaryDirectory() as td:
            seed = Path(td)
            self.assertEqual(run_git(["git", "-C", str(seed), "init"]).returncode, 0)
            self.assertEqual(run_git(["git", "-C", str(seed), "config", "user.name", "seed"]).returncode, 0)
            self.assertEqual(run_git(["git", "-C", str(seed), "config", "user.email", "seed@example.local"]).returncode, 0)
            (seed / "README.md").write_text("seed\n", encoding="utf-8")
            self.assertEqual(run_git(["git", "-C", str(seed), "add", "README.md"]).returncode, 0)
            self.assertEqual(run_git(["git", "-C", str(seed), "commit", "-m", "seed"]).returncode, 0)
            self.assertEqual(run_git(["git", "-C", str(seed), "remote", "add", "origin", str(target_bare)]).returncode, 0)
            self.assertEqual(run_git(["git", "-C", str(seed), "push", "origin", "HEAD:main"]).returncode, 0)

        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("icpc/problem.yaml", "problem_format_version: 2025-09\nname: reject stale target\n")
            zf.writestr("icpc/problem_statement/problem.tex", "\\section*{A}\n")
            zf.writestr("icpc/data/sample/1.in", "1\n")
            zf.writestr("icpc/data/sample/1.ans", "1\n")

        with self.assertRaisesRegex(ValueError, rf"import target already has revision history: {re.escape(target_problem)}"):
            import_package_as_new_problem(
                actor_user_id=int(actor_row["id"]),
                actor_user=str(actor_row["username"]),
                package_name="reject-stale-target.zip",
                package_content=package.getvalue(),
                requested_slug=target_slug,
                source_problem=self.problem,
            )

    def test_import_failure_cleans_up_half_created_problem(self) -> None:
        actor_row = db_fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"broken-import-{uuid.uuid4().hex[:8]}"
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "broken/problem.yaml",
                "problem_format_version: 2025-09\nname: broken\nvalidation: custom interactive\n",
            )
            zf.writestr("broken/domjudge-problem.ini", "short-name = broken\ntimelimit = 1\n")
            zf.writestr("broken/data/secret/001.in", "1\n")
            zf.writestr("broken/data/secret/001.ans", "1\n")
            zf.writestr("broken/submissions/accepted/std.cpp", "int main(){return 0;}\n")
        target_problem = f"{self.user}/{target_slug}"
        with self.assertRaisesRegex(ValueError, "missing output_validator/interactor source"):
            import_package_as_new_problem(
                actor_user_id=int(actor_row["id"]),
                actor_user=str(actor_row["username"]),
                package_name="broken-interactive-icpc.zip",
                package_content=package.getvalue(),
                requested_slug=target_slug,
                source_problem=self.problem,
            )
        self.assertIsNone(workspace_service.known_problem_id(target_problem))

    def test_export_respects_configured_validator_and_checker_sources(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        files = {
            "accepted": f"solutions/ac_cfg_{token}.cpp",
            "validator_selected": f"validators/z_validator_{token}.cpp",
            "validator_other": f"validators/a_validator_{token}.cpp",
            "checker_selected": f"checkers/z_checker_{token}.cpp",
            "checker_other": f"checkers/a_checker_{token}.cpp",
            "build_cfg": "config/build.json",
        }
        (ws / files["accepted"]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{files['accepted']}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / files["validator_selected"]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / files["validator_other"]).write_text("#include \"testlib.h\"\nint main(){return 1;}\n", encoding="utf-8")
        (ws / files["checker_selected"]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / files["checker_other"]).write_text("#include \"testlib.h\"\nint main(){return 1;}\n", encoding="utf-8")
        (ws / files["build_cfg"]).parent.mkdir(parents=True, exist_ok=True)
        (ws / files["build_cfg"]).write_text(
            "{\n"
            f"  \"validator_source\": \"{files['validator_selected']}\",\n"
            f"  \"checker_source\": \"{files['checker_selected']}\"\n"
            "}\n",
            encoding="utf-8",
        )

        tracked = [
            files["accepted"],
            f"{files['accepted']}.desc",
            files["validator_selected"],
            files["validator_other"],
            files["checker_selected"],
            files["checker_other"],
            files["build_cfg"],
            *self._seed_export_tests(ws, "001"),
        ]
        head = self._commit_workspace_paths(ws, tracked, f"test export cfg sources {token}")

        archive = export_service.create_export(
            self.problem,
            "",
            "icpc",
            workspace_id=int(workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["id"]),
            source_commit=head,
        )

        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("problem.yaml", names)
            self.assertFalse(any(name.startswith("input_validators/") for name in names))
            self.assertIn(f"output_validators/checker/{Path(files['checker_selected']).name}", names)
            self.assertNotIn(f"output_validators/checker/{Path(files['checker_other']).name}", names)

            content = zf.read("problem.yaml").decode("utf-8", errors="replace")
            self.assertNotIn("problem_format_version:", content)

    def test_export_keeps_only_latest_record_per_revision(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_latest_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export latest-per-revision {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        first = export_service.create_export(
            self.problem,
            "",
            "icpc",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )
        self.assertTrue(first.exists())
        second = export_service.create_export(
            self.problem,
            "",
            "icpc",
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=head,
        )
        self.assertTrue(second.exists())
        self.assertEqual(first.name, second.name)

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        rows = db_fetch_all(
            """
            SELECT id,verification_id,filename
            FROM exports
            WHERE problem_id=? AND workspace_id=? AND export_type='icpc' AND source_commit=?
            ORDER BY created_at DESC
            """,
            [problem_id, workspace_id, head],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["verification_id"]), "")

    def test_export_includes_statement_pdf_when_export_compile_succeeds(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_pdf_ok_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export statement pdf ok {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)

        def _compile_ok(_statement_root: Path, dst_statement: Path, *, problem_name: str) -> bool:
            self.assertTrue(str(problem_name or "").strip())
            dst_statement.mkdir(parents=True, exist_ok=True)
            (dst_statement / "problem.en.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            return True

        with patch.object(export_service, "_try_compile_statement_pdf", side_effect=_compile_ok) as compile_mock:
            archive = export_service.create_export(
                self.problem,
                "",
                "icpc",
                workspace_id=int(ctx["workspace"]["id"]),
                source_commit=head,
            )

        compile_mock.assert_called_once()
        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("problem.yaml", names)
        self.assertIn("problem_statement/problem.en.pdf", names)

    def test_export_statement_pdf_compilation_uses_shared_tex_compile_service(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_pdf_service_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export statement pdf shared compiler {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        calls: list[Path] = []

        def _fake_compile(tex_path: Path) -> TexCompileResult:
            calls.append(tex_path)
            pdf_path = tex_path.with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            (tex_path.parent / f"{tex_path.stem}.log").write_text("ok\n", encoding="utf-8")
            return TexCompileResult(
                engine="pdflatex",
                proc=ExecResult(
                    backend="fake",
                    status="ok",
                    returncode=0,
                    elapsed_ms=1,
                    timed_out=False,
                    stdout="",
                    stderr="",
                ),
                log_text="ok\n",
                pdf_path=pdf_path,
            )

        with patch.object(export_service.tex_compile_service, "compile_pdf", side_effect=_fake_compile) as compile_mock:
            archive = export_service.create_export(
                self.problem,
                "",
                "icpc",
                workspace_id=int(ctx["workspace"]["id"]),
                source_commit=head,
            )

        compile_mock.assert_called()
        self.assertTrue(calls)
        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("problem.yaml", names)
        self.assertIn("problem_statement/problem.en.pdf", names)

    def test_export_statement_pdf_uses_verification_sample_answer_artifacts(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_pdf_sample_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / "statement" / "problem.tex").write_text(DEFAULT_STATEMENT_PROBLEM_TEMPLATE, encoding="utf-8")
        (ws / "tests" / "manual" / "001.in").write_text("sample input\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps({"tests": [{"id": "001", "kind": "manual", "sample": True}]}, indent=2) + "\n",
            encoding="utf-8",
        )
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", "tests/manual/001.in", "tests/spec.json"],
            f"test export statement pdf sample artifact {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = f"ver-export-sample-pdf-{token}"
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature=head,
            source_commit=head,
            kind="all",
            status="ok",
            detail={"sanity_status": "passed", "validation_status": "passed"},
        )
        answer_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="answer",
            file_name="001.ans",
            payload=b"artifact sample answer\n",
        )
        config.verification_service.update_verification_artifact_refs(
            verification_id,
            "001.in",
            {"answer_ref": answer_ref},
        )

        def _fake_compile(tex_path: Path) -> TexCompileResult:
            rendered_root = tex_path.parent / "rendered" / "english"
            rendered_problem = (rendered_root / "problem.tex").read_text(encoding="utf-8")
            self.assertIn(r"\exmpfile{sample.001.in}{sample.001.ans}", rendered_problem)
            self.assertEqual((rendered_root / "sample.001.in").read_text(encoding="utf-8"), "sample input\n")
            self.assertEqual(
                (rendered_root / "sample.001.ans").read_text(encoding="utf-8"),
                "artifact sample answer\n",
            )
            pdf_path = tex_path.with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            return TexCompileResult(
                engine="pdflatex",
                proc=ExecResult(
                    backend="fake",
                    status="ok",
                    returncode=0,
                    elapsed_ms=1,
                    timed_out=False,
                    stdout="",
                    stderr="",
                ),
                log_text="ok\n",
                pdf_path=pdf_path,
            )

        with patch.object(export_service.tex_compile_service, "compile_pdf", side_effect=_fake_compile):
            archive = export_service.create_export(
                self.problem,
                verification_id,
                "icpc",
                workspace_id=int(ctx["workspace"]["id"]),
                source_commit=head,
            )

        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("problem_statement/problem.en.pdf", names)
            self.assertEqual(zf.read("data/sample/001.ans"), b"artifact sample answer\n")
        self.assertNotIn("sample_output", (ws / "tests" / "spec.json").read_text(encoding="utf-8"))

    def test_export_emits_multilanguage_statement_tex_and_pdf_names(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_multi_lang_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / "statement-sections" / "chinese").mkdir(parents=True, exist_ok=True)
        (ws / "statement-sections" / "chinese" / "name.tex").write_text("Chinese Title\n", encoding="utf-8")
        (ws / "statement-sections" / "chinese" / "legend.tex").write_text("Chinese Body\n", encoding="utf-8")
        (ws / "statement-sections" / "japanese").mkdir(parents=True, exist_ok=True)
        (ws / "statement-sections" / "japanese" / "name.tex").write_text("Japanese Title\n", encoding="utf-8")
        (ws / "statement-sections" / "japanese" / "legend.tex").write_text("Japanese Body\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [
                rel,
                f"{rel}.desc",
                *self._seed_export_tests(ws, "001"),
                "statement-sections/chinese/name.tex",
                "statement-sections/chinese/legend.tex",
                "statement-sections/japanese/name.tex",
                "statement-sections/japanese/legend.tex",
            ],
            f"test export statement multi language {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)

        def _compile_ok(_statement_root: Path, dst_statement: Path, *, problem_name: str) -> bool:
            self.assertTrue(str(problem_name or "").strip())
            dst_statement.mkdir(parents=True, exist_ok=True)
            for name in ("problem.en.pdf", "problem.zh.pdf", "problem.japanese.pdf"):
                (dst_statement / name).write_bytes(b"%PDF-1.4\n%%EOF\n")
            return True

        with patch.object(export_service, "_try_compile_statement_pdf", side_effect=_compile_ok):
            archive = export_service.create_export(
                self.problem,
                "",
                "icpc",
                workspace_id=int(ctx["workspace"]["id"]),
                source_commit=head,
            )

        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("problem.yaml", names)
        self.assertNotIn("problem_statement/problem.en.tex", names)
        self.assertNotIn("problem_statement/problem.zh.tex", names)
        self.assertNotIn("problem_statement/problem.japanese.tex", names)
        self.assertIn("problem_statement/problem.en.pdf", names)
        self.assertIn("problem_statement/problem.zh.pdf", names)
        self.assertIn("problem_statement/problem.japanese.pdf", names)

    def test_export_skips_statement_pdf_when_export_compile_fails(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_pdf_fail_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(
            ws,
            [rel, f"{rel}.desc", *self._seed_export_tests(ws, "001")],
            f"test export statement pdf fail {token}",
        )
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)

        with patch.object(export_service, "_try_compile_statement_pdf", return_value=False) as compile_mock:
            archive = export_service.create_export(
                self.problem,
                "",
                "icpc",
                workspace_id=int(ctx["workspace"]["id"]),
                source_commit=head,
            )

        compile_mock.assert_called_once()
        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("problem.yaml", names)
        self.assertNotIn("problem_statement/problem.en.pdf", names)
        self.assertNotIn("problem_statement/problem.en.tex", names)
