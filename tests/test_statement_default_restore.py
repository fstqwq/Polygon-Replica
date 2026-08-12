from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException

from app.impl.problem.file import files_page, files_restore_default
from app.main import runtime
from app.service.statement.constant import STATEMENT_DEFAULT_FILES

from tests.common import WorkspaceTestBase
from tests.ui_support import _flash_messages_from_response, _request


workspace_service = runtime.workspace_service


class TestStatementDefaultRestore(WorkspaceTestBase):
    def test_files_page_offers_restore_only_for_supported_text_paths(self) -> None:
        ws = self._workspace_path()
        action = f"/problems/{self.problem}/files/restore-default"
        for rel in STATEMENT_DEFAULT_FILES:
            with self.subTest(path=rel):
                html = self._files_html(rel, user=self.user, directory="statement")
                self.assertIn(f'action="{action}"', html)
                self.assertIn(f'name="path" value="{rel}"', html)

        for rel in ("statement/olymp.sty.bak", "notes/olymp.sty"):
            with self.subTest(path=rel):
                target = ws / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("unrelated\n", encoding="utf-8")
                self.assertNotIn(action, self._files_html(rel, user=self.user))

        supported = ws / "statement/olymp.sty"
        supported.unlink()
        self.assertIn(action, self._files_html("statement/olymp.sty", user=self.user))

        supported.write_bytes(b"\0binary")
        self.assertNotIn(action, self._files_html("statement/olymp.sty", user=self.user))

        supported.unlink()
        supported.mkdir()
        self.assertNotIn(action, self._files_html("statement/olymp.sty", user=self.user))

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
        rejected = {
            "statement/olymp.sty.bak": "default restore is not available",
            "notes/olymp.sty": "default restore is not available",
            "../statement/olymp.sty": "invalid path",
        }
        for rel, expected_message in rejected.items():
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
                self.assertIn(
                    expected_message,
                    _flash_messages_from_response(response)[0],
                )
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep\n")

        reader = self.random_id("restore-reader")
        workspace_service.ensure_user(reader)
        workspace_service.grant_repo_access(self.problem, reader, "read")
        reader_ws = Path(workspace_service.ensure_workspace(self.problem, reader))
        reader_target = reader_ws / "statement/olymp.sty"
        reader_target.parent.mkdir(parents=True, exist_ok=True)
        reader_target.write_text("reader custom\n", encoding="utf-8")
        html = self._files_html("statement/olymp.sty", user=reader)
        self.assertRegex(html, r'type="submit"[^>]*disabled')

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

    def _files_html(self, path: str, *, user: str, directory: str = "") -> str:
        query = f"path={path}"
        if directory:
            query += f"&dir={directory}"
        response = files_page(
            _request(f"/problems/{self.problem}/files", query),
            self.problem,
            user,
        )
        self.assertEqual(response.status_code, 200)
        return response.body.decode("utf-8", errors="replace")
