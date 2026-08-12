from __future__ import annotations

from unittest.mock import patch

from app.impl.workspace.problem_config import read_problem_config

from tests.common import E2ETestBase
from tests.ui_support import (
    Path,
    UIHelpersMixin,
    _flash_cookie_header,
    _flash_messages_from_response,
    _request,
    _request_with_cookie,
    checker_page,
    checker_rename_source,
    checker_save_source,
    checker_set_standard,
    checker_view_standard,
    files_save,
    general_page,
    generator_rename_source,
    generator_save_source,
    generators_page,
    interactor_page,
    interactor_rename_source,
    interactor_save_source,
    json,
    quote_plus,
    solutions_delete,
    solutions_editor_page,
    solutions_page,
    solutions_rename,
    solutions_save_source,
    solutions_set_tag,
    uuid,
    validator_page,
    validator_rename_source,
    validator_save_source,
    workspace_service,
)


class TestUIComponents(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = True
    seed_default_workspace = False

    @staticmethod
    def _update_build_config(ws: Path, **updates: object) -> None:
        path = ws / "config/build.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(updates)
        path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def test_read_problem_config_rejects_persisted_shape_without_pass_limit(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        cfg_path = ws / "config/problem.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps({"mode": "interactive"}, indent=2) + "\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "missing key"):
            read_problem_config(ws)

    def test_interactor_tab_visible_only_for_non_pass_fail_mode(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        cfg_path = ws / "config/problem.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        payload.update({"mode": "pass-fail", "pass_limit": 1})
        cfg_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        pass_fail_resp = general_page(
            _request(f"/problems/{self.problem}/general"), self.problem, self.user
        )
        self.assertEqual(pass_fail_resp.status_code, 200)
        pass_fail_html = pass_fail_resp.body.decode("utf-8", errors="replace")
        self.assertNotIn(f'/problems/{self.problem}/interactor', pass_fail_html)

        payload["mode"] = "interactive"
        cfg_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        interactive_resp = general_page(
            _request(f"/problems/{self.problem}/general"), self.problem, self.user
        )
        self.assertEqual(interactive_resp.status_code, 200)
        interactive_html = interactive_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f'/problems/{self.problem}/interactor', interactive_html)

    def test_checker_page_sets_standard_checker_metadata(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "checkers/checker.cpp"
        (ws / rel).write_text("// checker placeholder\n", encoding="utf-8")

        before = checker_page(
            _request(f"/problems/{self.problem}/checker"), self.problem, self.user
        )
        self.assertEqual(before.status_code, 200)

        install = checker_set_standard(
            problem=self.problem,
            user=self.user,
            checker_name="std::fcmp.cpp",
        )
        self.assertEqual(install.status_code, 303)
        loc = install.headers.get("location", "")
        self.assertIn(f"/problems/{self.problem}/checker", loc)

        build_cfg = json.loads(
            (ws / "config" / "build.json").read_text(encoding="utf-8")
        )
        self.assertEqual(build_cfg.get("checker_source"), "checkers/fcmp.cpp")
        self.assertNotIn("checker_standard", build_cfg)
        self.assertTrue((ws / "checkers/fcmp.cpp").exists())

        after = checker_page(
            _request(f"/problems/{self.problem}/checker"), self.problem, self.user
        )
        self.assertEqual(after.status_code, 200)
        after_html = after.body.decode("utf-8", errors="replace")
        self.assertIn("std::fcmp.cpp", after_html)

    def test_checker_page_warns_when_standard_checker_name_content_differs(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "checkers/wcmp.cpp"
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(
            "// custom checker with a standard name\n", encoding="utf-8"
        )
        self._update_build_config(ws, checker_source=rel)

        resp = checker_page(
            _request(f"/problems/{self.problem}/checker"), self.problem, self.user
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("File name matches std::wcmp.cpp, but content differs.", html)

    def test_checker_page_does_not_warn_when_custom_name_matches_standard_content(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        from app.service.verification.standard_checker import copy_standard_checker

        standard_rel = copy_standard_checker("wcmp.cpp", ws)
        custom_rel = "checkers/custom.cpp"
        (ws / custom_rel).write_bytes((ws / standard_rel).read_bytes())
        self._update_build_config(ws, checker_source=custom_rel)

        resp = checker_page(
            _request(f"/problems/{self.problem}/checker"), self.problem, self.user
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("std::wcmp.cpp", html)

    def test_checker_page_supports_source_save_without_files_page(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "checkers/checker.cpp"
        (ws / rel).unlink(missing_ok=True)

        resp = checker_page(
            _request(f"/problems/{self.problem}/checker"), self.problem, self.user
        )
        self.assertEqual(resp.status_code, 200)

        with patch(
            "app.impl.problem.checker.judgehost_compile_check_error", return_value=""
        ):
            saved = checker_save_source(
                problem=self.problem,
                user=self.user,
                path=rel,
                content="int main(int argc, char** argv){return argc > 0 ? 0 : 1;}\n",
            )
        self.assertEqual(saved.status_code, 303)
        self.assertIn("return argc", (ws / rel).read_text(encoding="utf-8"))

    def test_checker_save_source_compile_check_failure_keeps_standard_checker_source(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "checkers/checker.cpp"
        (ws / rel).unlink(missing_ok=True)

        set_standard = checker_set_standard(
            problem=self.problem, user=self.user, checker_name="std::fcmp.cpp"
        )
        self.assertEqual(set_standard.status_code, 303)
        cfg_before = json.loads(
            (ws / "config" / "build.json").read_text(encoding="utf-8")
        )
        self.assertEqual(cfg_before.get("checker_source"), "checkers/fcmp.cpp")
        self.assertTrue((ws / "checkers/fcmp.cpp").exists())

        failed = checker_save_source(
            problem=self.problem,
            user=self.user,
            path=rel,
            content="int main( { return 0; }\n",
        )
        self.assertEqual(failed.status_code, 303)
        self.assertEqual(
            failed.headers.get("location", ""), f"/problems/{self.problem}/checker"
        )
        messages = _flash_messages_from_response(failed)
        self.assertTrue(messages)
        self.assertIn("compile check failed", messages[0].lower())
        self.assertFalse((ws / rel).exists())

        cfg_after = json.loads(
            (ws / "config" / "build.json").read_text(encoding="utf-8")
        )
        self.assertEqual(cfg_after.get("checker_source"), "checkers/fcmp.cpp")
        self.assertNotIn("checker_standard", cfg_after)

    def test_checker_save_source_json_error_keeps_editor_content_local_only(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "checkers/checker_async_ce.cpp"
        source_abs = ws / rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        source_abs.write_text("int main(){return 0;}\n", encoding="utf-8")

        resp = checker_save_source(
            problem=self.problem,
            user=self.user,
            path=rel,
            content="int main( { return 0; }\n",
            response_mode="json",
        )
        self.assertEqual(resp.status_code, 400)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertFalse(bool(payload.get("ok")))
        self.assertIn("compile check failed", str(payload.get("error") or "").lower())
        self.assertEqual(
            source_abs.read_text(encoding="utf-8"), "int main(){return 0;}\n"
        )

    def test_generators_page_supports_drafts_and_source_save(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel_name = "gen.cpp"
        rel_second_name = "other.py"
        rel = f"generators/{rel_name}"
        rel_second = f"generators/{rel_second_name}"
        (ws / rel).unlink(missing_ok=True)
        (ws / rel_second).unlink(missing_ok=True)

        page = generators_page(
            _request(f"/problems/{self.problem}/generators"), self.problem, self.user
        )
        self.assertEqual(page.status_code, 200)
        empty_html = page.body.decode("utf-8", errors="replace")
        self.assertIn("No generators", empty_html)
        self.assertIn("?new=generator.cpp", empty_html)
        self.assertFalse((ws / rel).exists())

        draft = generators_page(
            _request(
                f"/problems/{self.problem}/generators",
                f"new={quote_plus(rel_name)}",
            ),
            self.problem,
            self.user,
        )
        draft_html = draft.body.decode("utf-8", errors="replace")
        self.assertIn("New generator", draft_html)
        self.assertIn("Insert testlib template", draft_html)
        self.assertIn("registerGen", draft_html)
        self.assertFalse((ws / rel).exists())

        with patch(
            "app.impl.problem.generator.judgehost_compile_check_error", return_value=""
        ):
            created = generator_save_source(
                problem=self.problem,
                user=self.user,
                path=rel,
                content=(
                    '#include "testlib.h"\n'
                    "int main(int argc,char** argv){"
                    "registerGen(argc, argv, 1); println(1); return 0;}\n"
                ),
            )
        self.assertEqual(created.status_code, 303)
        self.assertTrue((ws / rel).exists())
        page_after_create = generators_page(
            _request(f"/problems/{self.problem}/generators"), self.problem, self.user
        )
        self.assertEqual(page_after_create.status_code, 200)
        self.assertIn(
            "generators/save-source",
            page_after_create.body.decode("utf-8", errors="replace"),
        )

        cfg_path = ws / "config" / "build.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual([str(x) for x in cfg.get("generator_sources", [])], [rel])

        with patch(
            "app.impl.problem.generator.judgehost_compile_check_error", return_value=""
        ):
            saved = generator_save_source(
                problem=self.problem,
                user=self.user,
                path=rel,
                content=(
                    '#include "testlib.h"\n'
                    "int main(int argc,char** argv){"
                    "registerGen(argc, argv, 1); println(42); return 0;}\n"
                ),
            )
        self.assertEqual(saved.status_code, 303)
        self.assertIn("println(42)", (ws / rel).read_text(encoding="utf-8"))

        with patch(
            "app.impl.problem.generator.judgehost_compile_check_error", return_value=""
        ):
            saved_second = generator_save_source(
                problem=self.problem,
                user=self.user,
                path=rel_second,
                content="print(123)\n",
            )
        self.assertEqual(saved_second.status_code, 303)
        self.assertEqual((ws / rel_second).read_text(encoding="utf-8"), "print(123)\n")

        cfg_after_second = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [str(x) for x in cfg_after_second.get("generator_sources", [])],
            [rel, rel_second],
        )
        list_page = generators_page(
            _request(f"/problems/{self.problem}/generators"), self.problem, self.user
        )
        self.assertEqual(list_page.status_code, 200)

    def test_component_source_rename_updates_files_and_build_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        old_paths = {
            "checker": "checkers/old_checker.cpp",
            "validator": "validators/old_validator.cpp",
            "interactor": "interactors/old_interactor.cpp",
            "generator": "generators/old_generator.cpp",
        }
        for rel in old_paths.values():
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text(f"// {rel}\n", encoding="utf-8")
        other_generator = "generators/other.cpp"
        (ws / other_generator).write_text("// other generator\n", encoding="utf-8")
        cfg_path = ws / "config" / "build.json"
        self._update_build_config(
            ws,
            checker_source=old_paths["checker"],
            validator_source=old_paths["validator"],
            interactor_source=old_paths["interactor"],
            generator_sources=[old_paths["generator"], other_generator],
        )

        responses = [
            checker_rename_source(
                problem=self.problem,
                user=self.user,
                old_path=old_paths["checker"],
                new_path="new_checker.cpp",
            ),
            validator_rename_source(
                problem=self.problem,
                user=self.user,
                old_path=old_paths["validator"],
                new_path="new_validator.cpp",
            ),
            interactor_rename_source(
                problem=self.problem,
                user=self.user,
                old_path=old_paths["interactor"],
                new_path="new_interactor.cpp",
            ),
            generator_rename_source(
                problem=self.problem,
                user=self.user,
                old_path=old_paths["generator"],
                new_path="new_generator.cpp",
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 303)

        new_paths = {
            "checker": "checkers/new_checker.cpp",
            "validator": "validators/new_validator.cpp",
            "interactor": "interactors/new_interactor.cpp",
            "generator": "generators/new_generator.cpp",
        }
        for rel in old_paths.values():
            self.assertFalse((ws / rel).exists())
        for rel in new_paths.values():
            self.assertTrue((ws / rel).exists())

        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("checker_source"), new_paths["checker"])
        self.assertEqual(cfg.get("validator_source"), new_paths["validator"])
        self.assertEqual(cfg.get("interactor_source"), new_paths["interactor"])
        self.assertEqual(
            [str(x) for x in cfg.get("generator_sources", [])],
            [new_paths["generator"], other_generator],
        )

    def test_generator_save_source_compile_check_failure_does_not_persist_source_or_config(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "generators/gen_keep.cpp"
        rel_bad = "generators/gen_bad.cpp"
        (ws / rel).unlink(missing_ok=True)
        (ws / rel_bad).unlink(missing_ok=True)

        with patch(
            "app.impl.problem.generator.judgehost_compile_check_error", return_value=""
        ):
            ok_saved = generator_save_source(
                problem=self.problem,
                user=self.user,
                path=rel,
                content=(
                    '#include "testlib.h"\n'
                    "int main(int argc,char** argv){"
                    "registerGen(argc, argv, 1); println(7); return 0;}\n"
                ),
            )
        self.assertEqual(ok_saved.status_code, 303)
        self.assertIn("println(7)", (ws / rel).read_text(encoding="utf-8"))

        cfg_path = ws / "config" / "build.json"
        cfg_before = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [str(x) for x in cfg_before.get("generator_sources", [])], [rel]
        )

        with patch(
            "app.impl.problem.generator.judgehost_compile_check_error",
            return_value=f"{rel_bad}: syntax error",
        ):
            failed = generator_save_source(
                problem=self.problem,
                user=self.user,
                path=rel_bad,
                content='#include "testlib.h"\nint main( { return 0; }\n',
            )
        self.assertEqual(failed.status_code, 303)
        messages = _flash_messages_from_response(failed)
        self.assertTrue(messages)
        self.assertIn("compile check failed", messages[0].lower())
        self.assertFalse((ws / rel_bad).exists())

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [str(x) for x in cfg_after.get("generator_sources", [])], [rel]
        )

    def test_generator_save_source_json_success_returns_redirect(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "generators/gen_async_ok.cpp"
        content = (
            '#include "testlib.h"\n'
            "int main(int argc,char** argv){"
            "registerGen(argc, argv, 1); println(9); return 0;}\n"
        )
        with patch(
            "app.impl.problem.generator.judgehost_compile_check_error", return_value=""
        ):
            resp = generator_save_source(
                problem=self.problem,
                user=self.user,
                path=rel,
                content=content,
                response_mode="json",
            )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(
            str(payload.get("redirect") or ""),
            f"/problems/{self.problem}/generators?path=generators%2Fgen_async_ok.cpp",
        )
        self.assertEqual((ws / rel).read_text(encoding="utf-8"), content)

    def test_validator_editor_exposes_compile_error(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "validators/validator.cpp"
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")

        page = validator_page(
            _request_with_cookie(
                f"/problems/{self.problem}/validator",
                _flash_cookie_header(
                    "compile check failed: validators/validator.cpp: syntax error"
                ),
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(
            "compile check failed: validators/validator.cpp: syntax error", html
        )

    def test_checker_editor_exposes_compile_error_without_existing_repo_file(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "checkers/checker.cpp"
        target = ws / rel
        if target.exists():
            target.unlink()
        from app.service.verification.standard_checker import copy_standard_checker

        copy_standard_checker("wcmp.cpp", ws)
        self._update_build_config(ws, checker_source="checkers/wcmp.cpp")

        page = checker_page(
            _request_with_cookie(
                f"/problems/{self.problem}/checker",
                _flash_cookie_header(
                    "compile check failed: checkers/checker.cpp: syntax error"
                ),
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("compile check failed: checkers/checker.cpp: syntax error", html)

    def test_validator_editor_exposes_multiline_compile_error(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "validators/validator.cpp"
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")

        message = (
            "compile check failed: validators/validator.cpp: "
            "Compiling failed with exitcode 1, compiler output:\n"
            "validator.cpp:4:35: error: expected ';' before 'inf'"
        )
        page = validator_page(
            _request_with_cookie(
                f"/problems/{self.problem}/validator",
                _flash_cookie_header(message),
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Compiling failed with exitcode 1, compiler output:", html)
        self.assertIn("validator.cpp:4:35: error: expected", html)

    def test_generator_editor_exposes_compile_error(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "generators/gen.cpp"
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        self._update_build_config(ws, generator_sources=[rel])

        page = generators_page(
            _request_with_cookie(
                f"/problems/{self.problem}/generators",
                _flash_cookie_header(
                    "compile check failed: generators/gen.cpp: syntax error"
                ),
                f"path={quote_plus(rel)}",
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("compile check failed: generators/gen.cpp: syntax error", html)

    def test_generator_editor_exposes_compile_error_without_existing_file(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        rel = "generators/gen.cpp"
        target = ws / rel
        if target.exists():
            target.unlink()
        self._update_build_config(ws, generator_sources=[])

        page = generators_page(
            _request_with_cookie(
                f"/problems/{self.problem}/generators",
                _flash_cookie_header(
                    "compile check failed: generators/gen.cpp: syntax error"
                ),
                f"path={quote_plus(rel)}",
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("compile check failed: generators/gen.cpp: syntax error", html)

    def test_generators_page_hides_missing_configured_sources_and_fake_default_editor_path(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        missing_rel = "generators/generator.cpp"
        existing_rel = "generators/keep.cpp"
        missing_abs = ws / missing_rel
        if missing_abs.exists():
            missing_abs.unlink()
        existing_abs = ws / existing_rel
        existing_abs.parent.mkdir(parents=True, exist_ok=True)
        existing_abs.write_text("int main(){return 0;}\n", encoding="utf-8")
        self._update_build_config(
            ws, generator_sources=[missing_rel, existing_rel]
        )

        page = generators_page(
            _request(f"/problems/{self.problem}/generators"), self.problem, self.user
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(existing_rel, html)

        resp = general_page(
            _request(f"/problems/{self.problem}/general"), self.problem, self.user
        )
        self.assertEqual(resp.status_code, 200)

    def test_solutions_page_uses_desc_metadata_for_expected_behavior(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        source_rel = "solutions/std.cpp"
        source = ws / source_rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("int main(){return 0;}\n", encoding="utf-8")
        desc = ws / "solutions/std.cpp.desc"
        desc.write_text(
            "expected: wrong_answer\nnote: baseline negative case\n",
            encoding="utf-8",
        )

        page = solutions_page(
            _request(f"/problems/{self.problem}/solutions"), self.problem, self.user
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("solutions/std.cpp", html)
        self.assertIn("wrong_answer (WA)", html)

        set_tag = solutions_set_tag(
            problem=self.problem,
            user=self.user,
            source_path=source_rel,
            expected_behavior="accepted",
        )
        self.assertEqual(set_tag.status_code, 303)
        self.assertTrue(desc.exists())
        self.assertIn("expected: accepted", desc.read_text(encoding="utf-8"))

        after = general_page(
            _request(f"/problems/{self.problem}/general"), self.problem, self.user
        )
        self.assertEqual(after.status_code, 200)

    def test_solutions_page_links_to_blank_editor(self) -> None:
        page = solutions_page(
            _request(f"/problems/{self.problem}/solutions"), self.problem, self.user
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(
            f'/problems/{self.problem}/solutions/editor?path=solutions%2Faccepted.cpp',
            html,
        )

    def test_solutions_set_tag_main_correct_updates_main_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        token = uuid.uuid4().hex[:8]
        main_rel = f"solutions/foo_{token}.cpp"
        other_rel = f"solutions/bar_{token}.cpp"
        (ws / main_rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / other_rel).write_text("int main(){return 1;}\n", encoding="utf-8")
        cfg_path = ws / "config" / "build.json"

        before = solutions_page(
            _request(f"/problems/{self.problem}/solutions"), self.problem, self.user
        )
        self.assertEqual(before.status_code, 200)
        before_html = before.body.decode("utf-8", errors="replace")
        self.assertIn("Main correct solution is required.", before_html)

        resp = solutions_set_tag(
            problem=self.problem,
            user=self.user,
            source_path=main_rel,
            expected_behavior="main_correct",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"path={quote_plus(main_rel)}", resp.headers.get("location", ""))

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(str(cfg_after.get("accepted_solution_source") or ""), main_rel)

        after = solutions_page(
            _request(f"/problems/{self.problem}/solutions"), self.problem, self.user
        )
        self.assertEqual(after.status_code, 200)
        after_html = after.body.decode("utf-8", errors="replace")
        self.assertNotIn("Main correct solution is required.", after_html)

    def test_solutions_set_tag_accepted_does_not_select_main_correct_source(
        self,
    ) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        token = uuid.uuid4().hex[:8]
        source_rel = f"solutions/foo_{token}.cpp"
        (ws / source_rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        cfg_path = ws / "config" / "build.json"

        resp = solutions_set_tag(
            problem=self.problem,
            user=self.user,
            source_path=source_rel,
            expected_behavior="accepted",
        )
        self.assertEqual(resp.status_code, 303)

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("accepted_solution_source", cfg_after)

        page = solutions_page(
            _request(f"/problems/{self.problem}/solutions"), self.problem, self.user
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            "Main correct solution is required.",
            page.body.decode("utf-8", errors="replace"),
        )

    def test_solutions_rename_moves_desc_and_updates_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        token = uuid.uuid4().hex[:8]
        old_rel = f"solutions/rename_old_{token}.cpp"
        new_rel = f"solutions/rename_new_{token}.cpp"
        old_abs = ws / old_rel
        new_abs = ws / new_rel
        old_abs.parent.mkdir(parents=True, exist_ok=True)
        old_abs.write_text("int main(){return 1;}\n", encoding="utf-8")
        old_desc_abs = ws / f"{old_rel}.desc"
        old_desc_abs.write_text(
            "expected: wrong_answer\nnote: keep me\n", encoding="utf-8"
        )
        new_abs.unlink(missing_ok=True)
        (ws / f"{new_rel}.desc").unlink(missing_ok=True)

        cfg_path = ws / "config" / "build.json"
        self._update_build_config(ws, accepted_solution_source=old_rel)

        resp = solutions_rename(
            problem=self.problem,
            user=self.user,
            old_path=old_rel,
            new_path=Path(new_rel).name,
        )
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertIn(f"/problems/{self.problem}/solutions", location)
        self.assertIn(f"path={quote_plus(new_rel)}", location)

        self.assertFalse(old_abs.exists())
        self.assertTrue(new_abs.exists())
        self.assertFalse(old_desc_abs.exists())
        new_desc_abs = ws / f"{new_rel}.desc"
        self.assertTrue(new_desc_abs.exists())
        self.assertIn(
            "expected: wrong_answer", new_desc_abs.read_text(encoding="utf-8")
        )
        self.assertIn("note: keep me", new_desc_abs.read_text(encoding="utf-8"))

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(str(cfg_after.get("accepted_solution_source") or ""), new_rel)

    def test_solutions_delete_removes_source_desc_and_clears_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        token = uuid.uuid4().hex[:8]
        source_rel = f"solutions/delete_{token}.cpp"
        source_abs = ws / source_rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        source_abs.write_text("int main(){return 2;}\n", encoding="utf-8")
        desc_abs = ws / f"{source_rel}.desc"
        desc_abs.write_text("expected: accepted\n", encoding="utf-8")

        cfg_path = ws / "config" / "build.json"
        self._update_build_config(ws, accepted_solution_source=source_rel)

        resp = solutions_delete(
            problem=self.problem, user=self.user, source_path=source_rel
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(
            f"/problems/{self.problem}/solutions", resp.headers.get("location", "")
        )
        self.assertFalse(source_abs.exists())
        self.assertFalse(desc_abs.exists())

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("accepted_solution_source", cfg_after)

    def test_solutions_editor_page_saves_source_without_files_page(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        target_rel = "solutions/editor_case.cpp"
        (ws / target_rel).unlink(missing_ok=True)
        (ws / f"{target_rel}.desc").unlink(missing_ok=True)

        editor = solutions_editor_page(
            _request(
                f"/problems/{self.problem}/solutions/editor", f"path={target_rel}"
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(editor.status_code, 200)
        self.assertFalse((ws / target_rel).exists())
        editor_html = editor.body.decode("utf-8", errors="replace")
        self.assertIn('name="source_path" value="solutions/editor_case.cpp"', editor_html)

        updated_content = "int main(){return 42;}\n"
        saved = solutions_save_source(
            _request(
                f"/problems/{self.problem}/solutions/save-source", method="POST"
            ),
            problem=self.problem,
            user=self.user,
            source_path=target_rel,
            content=updated_content,
            expected_behavior="wrong_answer",
        )
        self.assertEqual(saved.status_code, 303)
        saved_location = saved.headers.get("location", "")
        self.assertIn("/solutions/editor", saved_location)
        self.assertIn("path=solutions%2Feditor_case.cpp", saved_location)
        self.assertEqual((ws / target_rel).read_text(encoding="utf-8"), updated_content)
        self.assertIn(
            "expected: wrong_answer",
            (ws / f"{target_rel}.desc").read_text(encoding="utf-8"),
        )
        messages = _flash_messages_from_response(saved)
        self.assertEqual(messages, ["solution source and metadata saved."])

        toast_page = solutions_editor_page(
            _request_with_cookie(
                f"/problems/{self.problem}/solutions/editor",
                _flash_cookie_header("solution source saved"),
                "path=solutions/editor_case.cpp",
            ),
            self.problem,
            self.user,
        )
        toast_html = toast_page.body.decode("utf-8", errors="replace")
        self.assertIn("solution source saved", toast_html)

    def test_solutions_save_source_ajax_success_returns_redirect(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        target_rel = "solutions/editor_ajax_ok_case.cpp"
        source_abs = ws / target_rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        source_abs.write_text("int main(){return 0;}\n", encoding="utf-8")

        req = _request(
            f"/problems/{self.problem}/solutions/save-source",
            method="POST",
            headers=[(b"x-requested-with", b"fetch"), (b"accept", b"application/json")],
        )
        updated_content = "int main(){return 7;}\n"
        resp = solutions_save_source(
            req,
            problem=self.problem,
            user=self.user,
            source_path=target_rel,
            content=updated_content,
            expected_behavior="accepted",
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(
            str(payload.get("redirect") or ""),
            f"/problems/{self.problem}/solutions/editor?path=solutions%2Feditor_ajax_ok_case.cpp",
        )
        self.assertEqual(source_abs.read_text(encoding="utf-8"), updated_content)
        self.assertIn(
            "expected: accepted",
            (ws / f"{target_rel}.desc").read_text(encoding="utf-8"),
        )
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("solution source", messages[0])

    def test_checker_view_standard_page_shows_source(self) -> None:
        resp = checker_view_standard(
            _request(
                f"/problems/{self.problem}/checker/view-standard",
                "checker_name=std%3A%3Afcmp.cpp",
            ),
            self.problem,
            self.user,
            checker_name="std::fcmp.cpp",
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("std::fcmp.cpp", html)
        self.assertIn("registerTestlibCmd", html)
        self.assertIn('data-code-editor="1"', html)
        self.assertIn("readonly", html)

    def test_validator_and_interactor_starters_remain_unsaved(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        validator_rel = "validators/validator.cpp"
        interactor_rel = "interactors/interactor.cpp"
        (ws / validator_rel).unlink(missing_ok=True)
        (ws / interactor_rel).unlink(missing_ok=True)

        validator_resp = validator_page(
            _request(f"/problems/{self.problem}/validator"), self.problem, self.user
        )
        self.assertEqual(validator_resp.status_code, 200)
        validator_html = validator_resp.body.decode("utf-8", errors="replace")
        self.assertNotIn("registerValidation", validator_html)
        self.assertFalse((ws / validator_rel).exists())

        validator_create_resp = validator_page(
            _request(
                f"/problems/{self.problem}/validator",
                "new=validator.cpp",
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(validator_create_resp.status_code, 200)
        validator_create_html = validator_create_resp.body.decode(
            "utf-8", errors="replace"
        )
        self.assertIn("registerValidation", validator_create_html)
        self.assertFalse((ws / validator_rel).exists())

        interactor_resp = interactor_page(
            _request(f"/problems/{self.problem}/interactor"), self.problem, self.user
        )
        self.assertEqual(interactor_resp.status_code, 200)
        interactor_html = interactor_resp.body.decode("utf-8", errors="replace")
        self.assertNotIn("registerInteraction", interactor_html)
        self.assertFalse((ws / interactor_rel).exists())

        interactor_create_resp = interactor_page(
            _request(
                f"/problems/{self.problem}/interactor",
                "new=interactor.cpp",
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(interactor_create_resp.status_code, 200)
        interactor_create_html = interactor_create_resp.body.decode(
            "utf-8", errors="replace"
        )
        self.assertIn("registerInteraction", interactor_create_html)
        self.assertFalse((ws / interactor_rel).exists())

        with patch(
            "app.impl.problem.checker.judgehost_compile_check_error", return_value=""
        ):
            checker_seed = checker_save_source(
                problem=self.problem,
                user=self.user,
                path="checkers/checker.cpp",
                content="int main(int argc, char** argv){return argc > 0 ? 0 : 1;}\n",
            )
        self.assertEqual(checker_seed.status_code, 303)
        checker_after_create = checker_page(
            _request(f"/problems/{self.problem}/checker"), self.problem, self.user
        )
        self.assertEqual(checker_after_create.status_code, 200)

        with patch(
            "app.impl.problem.validator.judgehost_compile_check_error", return_value=""
        ):
            validator_save = validator_save_source(
                problem=self.problem,
                user=self.user,
                path=validator_rel,
                content="int main(int argc, char** argv){return argc > 0 ? 0 : 1;}\n",
            )
        self.assertEqual(validator_save.status_code, 303)
        self.assertIn("return argc", (ws / validator_rel).read_text(encoding="utf-8"))

        with patch(
            "app.impl.problem.interactor.judgehost_compile_check_error", return_value=""
        ):
            interactor_save = interactor_save_source(
                problem=self.problem,
                user=self.user,
                path=interactor_rel,
                content="int main(int argc, char** argv){return argv != nullptr ? 0 : 1;}\n",
            )
        self.assertEqual(interactor_save.status_code, 303)
        self.assertIn("argv !=", (ws / interactor_rel).read_text(encoding="utf-8"))

    def test_validator_save_matches_files_save_replacement_semantics(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        validator_rel = "validators/validator.cpp"
        validator_abs = ws / validator_rel
        validator_abs.parent.mkdir(parents=True, exist_ok=True)
        validator_abs.write_text("int main(){return 0;}\n", encoding="utf-8")

        with patch(
            "app.impl.problem.validator.judgehost_compile_check_error", return_value=""
        ):
            validator_saved = validator_save_source(
                problem=self.problem,
                user=self.user,
                path=validator_rel,
                content="int main(){\r\n    return 7;\r\n}\r\n",
            )
        self.assertEqual(validator_saved.status_code, 303)
        self.assertEqual(validator_abs.read_bytes(), b"int main(){\n    return 7;\n}\n")

        files_saved = files_save(
            request=_request(f"/problems/{self.problem}/files/save"),
            problem=self.problem,
            user=self.user,
            path=validator_rel,
            content="int main(){\r\n    return 9;\r\n}\r\n",
        )
        self.assertEqual(files_saved.status_code, 303)
        self.assertEqual(validator_abs.read_bytes(), b"int main(){\n    return 9;\n}\n")

        with patch(
            "app.impl.problem.validator.judgehost_compile_check_error",
            return_value=f"{validator_rel}: compile check failed",
        ):
            emptied = validator_save_source(
                problem=self.problem,
                user=self.user,
                path=validator_rel,
                content="",
            )
        self.assertEqual(emptied.status_code, 303)
        # Empty validator source should fail compile-check and preserve the previous content.
        self.assertEqual(validator_abs.read_bytes(), b"int main(){\n    return 9;\n}\n")

        page = validator_page(
            _request(f"/problems/{self.problem}/validator"), self.problem, self.user
        )
        self.assertEqual(page.status_code, 200)
