from __future__ import annotations

from .db_helpers import db_execute, db_fetch_all, db_fetch_one

import io
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from .common import SmokeBase
from app.impl.run_export import export as export_page_module
from app.impl.run_export.import_source import import_package_as_new_problem
from app.impl.runtime.config import config
from app.service.importing import native as native_import_module
from app.service.platform.git_process import run_git
from app.service.importing.native import NATIVE_MARKER, NativePackageImportService

db = config.db
export_service = config.export_service
workspace_service = config.workspace_service


class TestExport(SmokeBase):
    def _seed_export_tests(self, workspace: Path, token: str) -> list[str]:
        tracked = [
            f"tests/manual/{token}.in",
            f"tests/answers/{token}.ans",
            "tests/spec.json",
        ]
        (workspace / tracked[0]).write_text("1\n", encoding="utf-8")
        (workspace / tracked[1]).write_text("1\n", encoding="utf-8")
        (workspace / tracked[2]).write_text(
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
        ]
        add = run_git(["git", "-C", str(workspace), "add", *(baseline + list(paths))])
        self.assertEqual(add.returncode, 0, add.stderr)
        commit = run_git(["git", "-C", str(workspace), "commit", "-m", message])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"])
        self.assertEqual(head.returncode, 0, head.stderr)
        return head.stdout.strip()

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

    def test_icpc_export_can_be_imported_as_new_problem(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        files = {
            "accepted": f"solutions/ac_roundtrip_{token}.cpp",
            "validator": f"validators/validator_roundtrip_{token}.cpp",
            "checker": f"checkers/checker_roundtrip_{token}.cpp",
            "build_cfg": "config/build.json",
        }
        (ws / files["accepted"]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{files['accepted']}.desc").write_text("expected: accepted\n", encoding="utf-8")
        (ws / files["validator"]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / files["checker"]).write_text("#include \"testlib.h\"\nint main(){return 42;}\n", encoding="utf-8")
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
        self.assertEqual((imported_ws / "tests" / "answers" / "001.ans").read_bytes(), b"1\n")
        self.assertTrue((imported_ws / "statement" / "statements.ftl").is_file())
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
            problem_yaml_name = next(name for name in zf.namelist() if name.endswith("/problem.yaml"))
            package_root = problem_yaml_name.split("/", 1)[0]
            problem_yaml = zf.read(problem_yaml_name).decode("utf-8", errors="replace")
            domjudge_ini = zf.read(f"{package_root}/domjudge-problem.ini").decode("utf-8", errors="replace")
            self.assertIn("validation: custom interactive multi-pass", problem_yaml)
            self.assertIn("validation_passes: 2", problem_yaml)
            self.assertIn("timelimit = 2", domjudge_ini)
            self.assertTrue(any(name.endswith("/output_validators/interactor/interactor_" + token + ".cpp") for name in zf.namelist()))

    def test_native_import_rejects_git_metadata_paths(self) -> None:
        ws = Path(self._workspace_path())
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                NATIVE_MARKER,
                json.dumps({"package_type": "native", "problem_name": "blocked git metadata"}),
            )
            zf.writestr("repo/.git/config", "[filter \"evil\"]\n")
            zf.writestr("repo/tests/spec.json", json.dumps({"tests": []}))

        service = NativePackageImportService()
        with self.assertRaisesRegex(ValueError, r"forbidden hidden path: repo/\.git/config"):
            service.import_package(ws, "native-git-metadata.zip", payload.getvalue())

    def test_native_import_rejects_hidden_workspace_paths(self) -> None:
        service = NativePackageImportService()
        blocked_paths = ["repo/.env", "repo/a/.hidden/file", "repo/.gitignore"]
        for blocked_path in blocked_paths:
            with self.subTest(blocked_path=blocked_path):
                ws = Path(self._workspace_path())
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(
                        NATIVE_MARKER,
                        json.dumps({"package_type": "native", "problem_name": "blocked hidden path"}),
                    )
                    zf.writestr(blocked_path, "hidden\n")
                    zf.writestr("repo/tests/spec.json", json.dumps({"tests": []}))

                with self.assertRaisesRegex(ValueError, rf"forbidden hidden path: {re.escape(blocked_path)}"):
                    service.import_package(ws, "native-hidden-path.zip", payload.getvalue())

    def test_native_import_rejects_total_unzipped_repo_payload_too_large(self) -> None:
        ws = Path(self._workspace_path())
        sentinel = ws / "keep.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                NATIVE_MARKER,
                json.dumps({"package_type": "native", "problem_name": "too large extracted payload"}),
            )
            zf.writestr("repo/a.txt", "1234567890")
            zf.writestr("repo/b.txt", "abcdefghij")

        service = NativePackageImportService()
        with patch.object(native_import_module, "ZIP_MAX_EXTRACTED_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "repo payload is too large"):
                service.import_package(ws, "native-too-large.zip", payload.getvalue())

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

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
            "tests/answers/001.ans",
            "tests/spec.json",
        ]
        (ws / tracked[0]).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / tracked[1]).write_text("expected: accepted\n", encoding="utf-8")
        (ws / tracked[2]).write_text("#include \"testlib.h\"\nint main(){return 0;}\n", encoding="utf-8")
        (ws / tracked[5]).write_text("1\n", encoding="utf-8")
        (ws / tracked[6]).write_text("1\n", encoding="utf-8")
        (ws / tracked[7]).write_text(
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
            problem_yaml = ""
            for name in names:
                if name.endswith("/problem.yaml"):
                    problem_yaml = name
                    break
            self.assertTrue(problem_yaml)
            package_root = problem_yaml.split("/", 1)[0]

            self.assertIn(f"{package_root}/input_validators/{Path(files['validator_selected']).name}", names)
            self.assertNotIn(f"{package_root}/input_validators/{Path(files['validator_other']).name}", names)
            self.assertIn(f"{package_root}/output_validators/checker/{Path(files['checker_selected']).name}", names)
            self.assertNotIn(f"{package_root}/output_validators/checker/{Path(files['checker_other']).name}", names)

            content = zf.read(problem_yaml).decode("utf-8", errors="replace")
            self.assertIn("problem_format_version:", content)

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

        def _compile_ok(_statement_root: Path, dst_statement: Path) -> bool:
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
        package_root = ""
        for name in names:
            if name.endswith("/problem.yaml"):
                package_root = name.split("/", 1)[0]
                break
        self.assertTrue(package_root)
        self.assertIn(f"{package_root}/statement/problem.en.pdf", names)

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
        package_root = ""
        for name in names:
            if name.endswith("/problem.yaml"):
                package_root = name.split("/", 1)[0]
                break
        self.assertTrue(package_root)
        self.assertNotIn(f"{package_root}/statement/problem.en.pdf", names)
        self.assertIn(f"{package_root}/statement/problem.en.tex", names)
