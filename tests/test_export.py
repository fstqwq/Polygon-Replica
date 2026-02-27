from __future__ import annotations

import re
import uuid
import zipfile
from pathlib import Path

from tests.common import SmokeBase
from app.impl.config import config
from app.services.util import run_cmd

db = config.db
export_service = config.export_service
workspace_service = config.workspace_service


class TestExport(SmokeBase):
    def _insert_exportable_build(self, build_id: str, source_commit: str) -> None:
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        artifact_root = self._artifact_root(build_id)
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
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                source_commit,
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )

    def _commit_workspace_paths(self, workspace: Path, paths: list[str], message: str) -> str:
        add = run_cmd(["git", "-C", str(workspace), "add", *paths])
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

    def test_icpc_export_maps_solution_expected_behaviors(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        files = {
            "accepted": f"solutions/ac_{token}.cpp",
            "wrong_answer": f"solutions/wa_{token}.cpp",
            "time_limit_exceeded": f"solutions/tle_{token}.cpp",
            "run_time_error": f"solutions/rte_{token}.cpp",
            "rejected": f"solutions/rej_{token}.cpp",
            "brute_force_alias": f"solutions/bf_{token}.cpp",
        }
        for expected, rel in files.items():
            src = ws / rel
            src.write_text("int main(){return 0;}\n", encoding="utf-8")
            if expected == "brute_force_alias":
                (ws / f"{rel}.desc").write_text("expected: brute_force\n", encoding="utf-8")
            else:
                (ws / f"{rel}.desc").write_text(f"expected: {expected}\n", encoding="utf-8")

        tracked: list[str] = []
        for rel in files.values():
            tracked.append(rel)
            tracked.append(f"{rel}.desc")
        head = self._commit_workspace_paths(ws, tracked, f"test export keywords {token}")

        build_id = f"b-exp-{token}"
        self._insert_exportable_build(build_id, head)
        archive = export_service.create_export(self.problem, build_id, "icpc")
        self.assertRegex(archive.name, rf"^{re.escape(self.problem)}-v\d+\.zip$")

        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
        package_root = ""
        for name in names:
            if name.endswith("/problem.yaml"):
                package_root = name.split("/", 1)[0]
                break
        self.assertTrue(package_root)

        self.assertIn(f"{package_root}/submissions/accepted/{Path(files['accepted']).name}", names)
        self.assertIn(f"{package_root}/submissions/wrong_answer/{Path(files['wrong_answer']).name}", names)
        self.assertIn(f"{package_root}/submissions/time_limit_exceeded/{Path(files['time_limit_exceeded']).name}", names)
        self.assertIn(f"{package_root}/submissions/run_time_error/{Path(files['run_time_error']).name}", names)
        self.assertIn(f"{package_root}/submissions/rejected/{Path(files['rejected']).name}", names)
        self.assertIn(f"{package_root}/submissions/time_limit_exceeded/{Path(files['brute_force_alias']).name}", names)
        self.assertIn(f"{package_root}/problem.yaml", names)

    def test_export_rejects_non_icpc_type(self) -> None:
        ws = Path(self._workspace_path())
        token = uuid.uuid4().hex[:8]
        rel = f"solutions/ac_only_{token}.cpp"
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / f"{rel}.desc").write_text("expected: accepted\n", encoding="utf-8")
        head = self._commit_workspace_paths(ws, [rel, f"{rel}.desc"], f"test export type reject {token}")
        build_id = f"b-exp-reject-{token}"
        self._insert_exportable_build(build_id, head)
        with self.assertRaisesRegex(ValueError, "ICPC only"):
            export_service.create_export(self.problem, build_id, "kattis")

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

        build_id = f"b-exp-cfg-{token}"
        self._insert_exportable_build(build_id, head)
        archive = export_service.create_export(self.problem, build_id, "icpc")

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

        build_id_1 = f"b-exp-latest-a-{token}"
        build_id_2 = f"b-exp-latest-b-{token}"
        self._insert_exportable_build(build_id_1, head)
        self._insert_exportable_build(build_id_2, head)

        first = export_service.create_export(self.problem, build_id_1, "icpc")
        self.assertTrue(first.exists())
        second = export_service.create_export(self.problem, build_id_2, "icpc")
        self.assertTrue(second.exists())
        self.assertEqual(first.name, second.name)

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        rows = db.fetch_all(
            """
            SELECT id,build_id,filename
            FROM exports
            WHERE problem_id=? AND workspace_id=? AND export_type='icpc' AND source_commit=?
            ORDER BY created_at DESC
            """,
            [problem_id, workspace_id, head],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["build_id"]), build_id_2)
