from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from tests.common import SmokeBase, testsuite_root
from app.impl.config import config
from app.impl.root_auth import auth_password_meta, login_page, register_submit
from app.impl.run_export import artifact_file

build_service = config.build_service
db = config.db
run_service = config.run_service
toolchain_service = config.toolchain_service
workspace_service = config.workspace_service


def _request(
    path: str,
    query: str = "",
    *,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "headers": headers or [],
            "client": ("127.0.0.1", 0),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        }
    )


def _post_request(path: str, *, origin: str = "http://testserver") -> Request:
    return _request(path, method="POST", headers=[(b"origin", origin.encode("utf-8"))])


def _extract_hidden_input_value(html: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="([^"]*)"', html, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


class TestSecurity(SmokeBase):
    def _prepare_guarded_flag_build(self, problem: str, user: str, flag_text: str) -> tuple[Path, str, Path, Path]:
        if shutil.which("g++") is None:
            self.skipTest("g++ is required for checker compile")
        workspace_service.ensure_problem(problem, "Security Guard Problem")
        workspace_service.grant_repo_access(problem, user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, user))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)

        ctx = workspace_service.workspace_context(problem, user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-sec-flag-{uuid.uuid4().hex[:8]}"
        artifact_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_root / "tests" / "001.in").write_text("0\n", encoding="utf-8")
        ans_path = artifact_root / "ans" / "001.ans"
        ans_path.write_text(flag_text + "\n", encoding="utf-8")
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "max_passes": 1,
                    "run_jobs": 1,
                    "time_limit_ms": 200,
                    "run_memory_mb": 512,
                    "run_process_limit": 64,
                    "run_output_kb": 16384,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        checker_src = Path("third_party/upstream/testlib/checkers/wcmp.cpp")
        checker_bin = artifact_root / "bin" / "checker"
        ok, cout, cerr, _ = toolchain_service.compile_cpp(
            checker_src,
            checker_bin,
            include_dirs=[Path("third_party/upstream/testlib")],
            path_roots=[Path("."), Path("third_party/upstream/testlib")],
        )
        self.assertTrue(ok, msg=f"checker compile failed\nstdout:\n{cout}\nstderr:\n{cerr}")

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-25T00:10:00Z",
                "2026-02-25T00:10:01Z",
            ],
        )
        return ws, build_id, artifact_root, ans_path

    def _assert_escape_attempt_not_ok(
        self,
        *,
        language: str,
        source_rel: str,
        source_code: str,
        require_java: bool = False,
    ) -> None:
        if require_java and (shutil.which("javac") is None or shutil.which("java") is None):
            self.skipTest("javac/java is not available")

        problem = f"sec-escape-{language}-{uuid.uuid4().hex[:8]}"
        flag_text = f"FLAG{{secret-{uuid.uuid4().hex[:8]}}}"
        ws, build_id, _artifact_root, ans_path = self._prepare_guarded_flag_build(problem, self.user, flag_text)
        source_path = ws / source_rel
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(source_code.format(ans_path=ans_path.as_posix()), encoding="utf-8")

        run_id = run_service.run_submission(
            problem,
            self.user,
            build_id,
            submission_path=source_rel,
            mode="pass-fail",
        )
        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        status = str(row["status"] or "")
        summary = json.loads(str(row["summary_json"] or "{}"))
        self.assertEqual(status, "ok", msg=f"unexpected run status={status}, summary={json.dumps(summary, ensure_ascii=False)}")
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertTrue(tests)
        verdict = str(tests[0].get("verdict") or "")
        self.assertIn(verdict, {"WA", "RE"})

    def test_auth_password_meta_ignores_sql_injection_style_username(self) -> None:
        username = f"secsql-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        registered = register_submit(
            request=_post_request("/register"),
            username=username,
            password=password,
            password_confirm=password,
            next="/",
        )
        self.assertEqual(registered.status_code, 303)

        row = db.fetch_one("SELECT password_salt FROM users WHERE username=?", [username])
        self.assertIsNotNone(row)
        real_salt = str(row["password_salt"] or "").strip().lower()
        self.assertRegex(real_salt, r"^[0-9a-f]{32}$")

        login_resp = login_page(_request("/login"))
        self.assertEqual(login_resp.status_code, 200)
        login_html = login_resp.body.decode("utf-8", errors="replace")
        csrf = _extract_hidden_input_value(login_html, "csrf_token")
        self.assertTrue(csrf)

        normal_meta = auth_password_meta(username=username, csrf_token=csrf)
        self.assertEqual(str(normal_meta.get("salt") or "").strip().lower(), real_salt)

        injected_username = f"{username}' OR 1=1 --"
        injected_meta = auth_password_meta(username=injected_username, csrf_token=csrf)
        injected_salt = str(injected_meta.get("salt") or "").strip().lower()
        self.assertRegex(injected_salt, r"^[0-9a-f]{32}$")
        self.assertNotEqual(injected_salt, real_salt)

    def test_artifact_download_denies_cross_workspace_access(self) -> None:
        workspace_service.grant_repo_access("sample", "bob", "owner")
        workspace_service.ensure_workspace("sample", "bob")

        alice_ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])

        build_id = f"b-sec-artifact-{uuid.uuid4().hex[:8]}"
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs" / "compile.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                alice_workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-25T00:00:00Z",
                "2026-02-25T00:00:01Z",
            ],
        )

        with self.assertRaises(HTTPException) as denied:
            artifact_file("sample", "bob", build_id, "logs/compile.log")
        self.assertEqual(denied.exception.status_code, 404)
        self.assertIn("workspace", str(denied.exception.detail))

    def test_artifact_download_rejects_path_traversal(self) -> None:
        alice_ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])

        build_id = f"b-sec-path-{uuid.uuid4().hex[:8]}"
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs" / "compile.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                alice_workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-25T00:00:00Z",
                "2026-02-25T00:00:01Z",
            ],
        )

        with self.assertRaises(HTTPException) as denied:
            artifact_file("sample", "alice", build_id, "../outside.txt")
        self.assertEqual(denied.exception.status_code, 400)
        self.assertIn("invalid artifact path", str(denied.exception.detail))

    def test_tests_spec_gen_command_shell_tokens_do_not_escape(self) -> None:
        problem = f"secgen-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, "Security Generator")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "checkers",
            "solutions",
            "generators",
            "tests/generator",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "third_party" / "testlib" / "testlib.h").write_text("// testlib placeholder\n", encoding="utf-8")
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "accepted_solution_source": "solutions/accepted.cpp",
                    "compile_jobs": 1,
                    "validate_jobs": 1,
                    "solve_jobs": 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "validators" / "validator.cpp").write_text(
            """#include <iostream>
int main() {
    long long x = 0;
    if (!(std::cin >> x)) return 3;
    return 0;
}
""",
            encoding="utf-8",
        )
        (ws / "checkers" / "checker.cpp").write_text(
            """int main() { return 0; }
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() {
    long long x = 0;
    if (!(std::cin >> x)) return 0;
    std::cout << x << "\\n";
    return 0;
}
""",
            encoding="utf-8",
        )
        (ws / "generators" / "gen.cpp").write_text(
            """#include <iostream>
int main() {
    std::cout << 7 << "\\n";
    return 0;
}
""",
            encoding="utf-8",
        )
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "tests": [
                        {"id": "001", "kind": "gen", "sample": False},
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        marker = testsuite_root() / f"compile-escape-{uuid.uuid4().hex[:8]}.txt"
        marker.unlink(missing_ok=True)
        injected_cmd = f"gen.cpp 7 && touch {marker.as_posix()}"
        (ws / "tests" / "generator" / "001.in").write_text(injected_cmd + "\n", encoding="utf-8")

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,artifact_path FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "ok")
        self.assertFalse(marker.exists())

        artifact_path = Path(str(row["artifact_path"] or ""))
        built_input = artifact_path / "tests" / "001.in"
        self.assertTrue(built_input.exists())
        self.assertEqual(built_input.read_text(encoding="utf-8").strip(), "7")

    def test_submission_cpp_cannot_read_flag_from_answer_path(self) -> None:
        self._assert_escape_attempt_not_ok(
            language="cpp",
            source_rel="solutions/escape.cpp",
            source_code=(
                "#include <fstream>\n"
                "#include <iostream>\n"
                "#include <string>\n"
                "int main() {{\n"
                "    std::ifstream in(\"{ans_path}\");\n"
                "    std::string s;\n"
                "    if (std::getline(in, s) && !s.empty()) {{\n"
                "        std::cout << s << \"\\n\";\n"
                "    }} else {{\n"
                "        std::cout << \"fallback\" << \"\\n\";\n"
                "    }}\n"
                "    return 0;\n"
                "}}\n"
            ),
        )

    def test_submission_python_cannot_read_flag_from_answer_path(self) -> None:
        self._assert_escape_attempt_not_ok(
            language="python",
            source_rel="solutions/escape.py",
            source_code=(
                "from pathlib import Path\n"
                "p = Path('{ans_path}')\n"
                "try:\n"
                "    text = p.read_text(encoding='utf-8').strip()\n"
                "    print(text if text else 'fallback')\n"
                "except Exception:\n"
                "    print('fallback')\n"
            ),
        )

    def test_submission_java_cannot_read_flag_from_answer_path(self) -> None:
        self._assert_escape_attempt_not_ok(
            language="java",
            source_rel="solutions/Main.java",
            require_java=True,
            source_code=(
                "import java.nio.file.Files;\n"
                "import java.nio.file.Path;\n"
                "public class Main {{\n"
                "    public static void main(String[] args) {{\n"
                "        try {{\n"
                "            String s = Files.readString(Path.of(\"{ans_path}\")).trim();\n"
                "            if (s.isEmpty()) {{\n"
                "                System.out.println(\"fallback\");\n"
                "            }} else {{\n"
                "                System.out.println(s);\n"
                "            }}\n"
                "        }} catch (Exception ex) {{\n"
                "            System.out.println(\"fallback\");\n"
                "        }}\n"
                "    }}\n"
                "}}\n"
            ),
        )
