from __future__ import annotations

import io
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from .common import SmokeBase
from app.impl.run_export.import_source import import_package_as_new_problem
from app.impl.runtime.config import config
from app.service.platform.process import run_cmd

db = config.db
export_service = config.export_service
workspace_service = config.workspace_service


class TestExport(SmokeBase):
    def _insert_exportable_verification(self, verification_id: str, source_commit: str) -> None:
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_ref = config.fs_manager.compute_artifact_ref(
            {"suite": "export", "problem": self.problem, "verification_id": str(verification_id or "").strip()}
        )
        artifact_root = config.fs_manager.ensure_artifact_layout(build_ref).root.resolve()
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

        db.execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                problem_id,
                workspace_id,
                source_commit,
                "main",
                "verification",
                "ok",
                "{}",
                str(artifact_root),
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
        add = run_cmd(["git", "-C", str(workspace), "add", *(baseline + list(paths))])
        self.assertEqual(add.returncode, 0, add.stderr)
        commit = run_cmd(["git", "-C", str(workspace), "commit", "-m", message])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"])
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
        with self.assertRaisesRegex(ValueError, "ICPC only"):
            export_service.create_export(self.problem, verification_id, "kattis")

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
            ],
            f"test export import roundtrip {token}",
        )
        verification_id = f"ver-exp-imp-{token}"
        self._insert_exportable_verification(verification_id, head)
        archive = export_service.create_export(self.problem, verification_id, "icpc")

        actor_row = db.fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
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
        self.assertIn(str(imported_problem_cfg.get("mode") or ""), {"pass-fail", "interactive", "multi-pass"})

    def test_polygon_import_builds_missing_sample_answers_via_verification(self) -> None:
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

        actor_row = db.fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"poly-backfill-{uuid.uuid4().hex[:8]}"
        target_problem = f"{self.user}/{target_slug}"

        def _fake_run_build(problem: str, username: str, *args, **kwargs) -> str:
            self.assertEqual(problem, target_problem)
            self.assertEqual(username, self.user)
            verification_id = f"ver-backfill-{uuid.uuid4().hex[:8]}"
            target_ctx = workspace_service.workspace_context(target_problem, self.user, include_recent=False)
            build_ref = config.fs_manager.compute_artifact_ref(
                {"suite": "export-backfill", "problem": target_problem, "verification_id": verification_id}
            )
            artifact_root = config.fs_manager.ensure_artifact_layout(build_ref).root.resolve()
            (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
            (artifact_root / "ans" / "001.ans").write_text("7\n", encoding="utf-8")
            db.execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    int(target_ctx["problem"]["id"]),
                    int(target_ctx["workspace"]["id"]),
                    "",
                    "main",
                    "verification",
                    "ok",
                    "{}",
                    str(artifact_root),
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return verification_id

        with patch("app.impl.run_export.import_source.config.verification_service.run_verification", side_effect=_fake_run_build):
            imported = import_package_as_new_problem(
                actor_user_id=int(actor_row["id"]),
                actor_user=str(actor_row["username"]),
                package_name="sample-backfill.zip",
                package_content=payload.getvalue(),
                requested_slug=target_slug,
                source_problem=self.problem,
            )
        target_problem = str(imported.get("target_problem") or "")
        imported_ws = Path(workspace_service.ensure_workspace(target_problem, self.user))
        answer_path = imported_ws / "tests" / "answers" / "001.ans"
        self.assertTrue(answer_path.is_file())
        self.assertEqual(answer_path.read_text(encoding="utf-8"), "7\n")
        tests_summary = imported.get("result", {}).get("tests", {})
        self.assertEqual(int(tests_summary.get("sample_answers_built") or 0), 1)

    def test_polygon_import_fails_when_sample_answer_build_verification_fails(self) -> None:
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="sample-backfill-fail">
  <names>
    <name language="english" value="Sample Answer Backfill Fail"/>
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
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", "1\n")

        actor_row = db.fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"poly-backfail-{uuid.uuid4().hex[:8]}"
        with self.assertRaisesRegex(ValueError, "sample answer verification failed"):
            import_package_as_new_problem(
                actor_user_id=int(actor_row["id"]),
                actor_user=str(actor_row["username"]),
                package_name="sample-backfill-fail.zip",
                package_content=payload.getvalue(),
                requested_slug=target_slug,
                source_problem=self.problem,
            )

    def test_import_refuses_target_with_existing_revision_history(self) -> None:
        actor_row = db.fetch_one("SELECT id,username FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor_row)
        target_slug = f"stale-import-{uuid.uuid4().hex[:8]}"
        target_problem = f"{self.user}/{target_slug}"
        target_bare = (config.settings.bare_root / f"{target_problem}.git").resolve()
        target_bare.parent.mkdir(parents=True, exist_ok=True)
        init = run_cmd(["git", "init", "--bare", str(target_bare)])
        self.assertEqual(init.returncode, 0, init.stderr or init.stdout)

        with tempfile.TemporaryDirectory() as td:
            seed = Path(td)
            self.assertEqual(run_cmd(["git", "-C", str(seed), "init"]).returncode, 0)
            self.assertEqual(run_cmd(["git", "-C", str(seed), "config", "user.name", "seed"]).returncode, 0)
            self.assertEqual(run_cmd(["git", "-C", str(seed), "config", "user.email", "seed@example.local"]).returncode, 0)
            (seed / "README.md").write_text("seed\n", encoding="utf-8")
            self.assertEqual(run_cmd(["git", "-C", str(seed), "add", "README.md"]).returncode, 0)
            self.assertEqual(run_cmd(["git", "-C", str(seed), "commit", "-m", "seed"]).returncode, 0)
            self.assertEqual(run_cmd(["git", "-C", str(seed), "remote", "add", "origin", str(target_bare)]).returncode, 0)
            self.assertEqual(run_cmd(["git", "-C", str(seed), "push", "origin", "HEAD:main"]).returncode, 0)

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
        ]
        head = self._commit_workspace_paths(ws, tracked, f"test export cfg sources {token}")

        verification_id = f"ver-exp-cfg-{token}"
        self._insert_exportable_verification(verification_id, head)
        archive = export_service.create_export(self.problem, verification_id, "icpc")

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
            self.assertIn(f"{package_root}/output_validator/{Path(files['checker_selected']).name}", names)
            self.assertNotIn(f"{package_root}/output_validator/{Path(files['checker_other']).name}", names)

            content = zf.read(problem_yaml).decode("utf-8", errors="replace")
            self.assertIn("problem_format_version:", content)

    def test_export_keeps_only_latest_record_per_revision(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_latest_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(ws, [rel, f"{rel}.desc"], f"test export latest-per-revision {token}")

        verification_id_1 = f"ver-exp-latest-a-{token}"
        verification_id_2 = f"ver-exp-latest-b-{token}"
        self._insert_exportable_verification(verification_id_1, head)
        self._insert_exportable_verification(verification_id_2, head)

        first = export_service.create_export(self.problem, verification_id_1, "icpc")
        self.assertTrue(first.exists())
        second = export_service.create_export(self.problem, verification_id_2, "icpc")
        self.assertTrue(second.exists())
        self.assertEqual(first.name, second.name)

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        rows = db.fetch_all(
            """
            SELECT id,verification_id,filename
            FROM exports
            WHERE problem_id=? AND workspace_id=? AND export_type='icpc' AND source_commit=?
            ORDER BY created_at DESC
            """,
            [problem_id, workspace_id, head],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["verification_id"]), verification_id_2)

    def test_export_includes_statement_pdf_when_export_compile_succeeds(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_pdf_ok_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(ws, [rel, f"{rel}.desc"], f"test export statement pdf ok {token}")

        verification_id = f"ver-exp-pdf-ok-{token}"
        self._insert_exportable_verification(verification_id, head)

        def _compile_ok(_statement_root: Path, dst_statement: Path) -> bool:
            dst_statement.mkdir(parents=True, exist_ok=True)
            (dst_statement / "problem.en.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            return True

        with patch.object(export_service, "_try_compile_statement_pdf", side_effect=_compile_ok) as compile_mock:
            archive = export_service.create_export(self.problem, verification_id, "icpc")

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
        head = self._commit_workspace_paths(ws, [rel, f"{rel}.desc"], f"test export statement pdf fail {token}")

        verification_id = f"ver-exp-pdf-fail-{token}"
        self._insert_exportable_verification(verification_id, head)

        with patch.object(export_service, "_try_compile_statement_pdf", return_value=False) as compile_mock:
            archive = export_service.create_export(self.problem, verification_id, "icpc")

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
