from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException

from app.impl.problem.file import files_restore_default
from app.main import runtime
from app.service.statement.constant import STATEMENT_DEFAULT_FILES

from tests.common import WorkspaceTestBase
from tests.ui_support import _request


workspace_service = runtime.workspace_service


class TestStatementDefaultRestore(WorkspaceTestBase):
    def test_restore_is_exact_and_recreates_missing_file(self) -> None:
        ws = self._workspace_path()
        custom = {
            rel: f"custom {index}\n"
            for index, rel in enumerate(STATEMENT_DEFAULT_FILES)
        }
        for selected, expected in STATEMENT_DEFAULT_FILES.items():
            with self.subTest(path=selected):
                for rel, content in custom.items():
                    target = ws / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")

                response = files_restore_default(
                    request=_request(
                        f"/problems/{self.problem}/files/restore-default"
                    ),
                    problem=self.problem,
                    user=self.user,
                    path=selected,
                    dir="statement",
                )

                self.assertEqual(response.status_code, 303)
                query = parse_qs(urlparse(response.headers["location"]).query)
                self.assertEqual(query, {"path": [selected], "dir": ["statement"]})
                self.assertEqual((ws / selected).read_text(encoding="utf-8"), expected)
                for rel, content in custom.items():
                    if rel != selected:
                        self.assertEqual((ws / rel).read_text(encoding="utf-8"), content)
        missing = "statement/olymp.sty"
        (ws / missing).unlink()
        files_restore_default(
            request=_request(f"/problems/{self.problem}/files/restore-default"),
            problem=self.problem,
            user=self.user,
            path=missing,
        )
        self.assertEqual(
            (ws / missing).read_text(encoding="utf-8"),
            STATEMENT_DEFAULT_FILES[missing],
        )

    def test_restore_rejects_other_paths_and_read_only_users(self) -> None:
        ws = self._workspace_path()
        unrelated = ws / "statement/olymp.sty.bak"
        unrelated.write_text("keep\n", encoding="utf-8")
        default_target = ws / "statement/olymp.sty"
        default_target.write_text("custom default\n", encoding="utf-8")
        rejected = (
            "statement/olymp.sty.bak",
            "notes/olymp.sty",
            "../statement/olymp.sty",
        )
        for rel in rejected:
            with self.subTest(path=rel):
                response = files_restore_default(
                    request=_request(
                        f"/problems/{self.problem}/files/restore-default"
                    ),
                    problem=self.problem,
                    user=self.user,
                    path=rel,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep\n")
                self.assertEqual(
                    default_target.read_text(encoding="utf-8"),
                    "custom default\n",
                )

        reader = self.random_id("restore-reader")
        workspace_service.ensure_user(reader)
        workspace_service.grant_repo_access(self.problem, reader, "read")
        reader_ws = Path(workspace_service.ensure_workspace(self.problem, reader))
        reader_target = reader_ws / "statement/olymp.sty"
        reader_target.parent.mkdir(parents=True, exist_ok=True)
        reader_target.write_text("reader custom\n", encoding="utf-8")
        with self.assertRaises(HTTPException) as denied:
            files_restore_default(
                request=_request(
                    f"/problems/{self.problem}/files/restore-default"
                ),
                problem=self.problem,
                user=reader,
                path="statement/olymp.sty",
            )
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(
            reader_target.read_text(encoding="utf-8"),
            "reader custom\n",
        )
