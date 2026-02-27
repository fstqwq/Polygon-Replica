from __future__ import annotations

from tests.ui_support import (
    Path,
    UIBaseSuite,
    _flash_cookie_header,
    _flash_messages_from_response,
    _request,
    _request_with_cookie,
    checker_page,
    checker_save_source,
    checker_set_standard,
    checker_view_standard,
    general_page,
    generator_create_template,
    generator_save_source,
    generators_page,
    interactor_create_template,
    interactor_page,
    interactor_save_source,
    json,
    quote_plus,
    re,
    solutions_create_template,
    solutions_delete,
    solutions_editor_page,
    solutions_page,
    solutions_rename,
    solutions_save_source,
    solutions_set_tag,
    uuid,
    validator_create_template,
    validator_page,
    validator_save_source,
    workspace_service,
)


class TestUIComponents(UIBaseSuite):
    def test_interactor_tab_visible_only_for_non_pass_fail_mode(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        cfg_path = ws / "config/problem.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        cfg_path.write_text(json.dumps({"mode": "pass-fail"}, indent=2) + "\n", encoding="utf-8")
        pass_fail_resp = general_page(_request("/problems/sample/alice/general"), "sample", "alice")
        self.assertEqual(pass_fail_resp.status_code, 200)
        pass_fail_html = pass_fail_resp.body.decode("utf-8", errors="replace")
        self.assertIn("/problems/sample/alice/preview", pass_fail_html)
        self.assertNotIn(">Interactor</span>", pass_fail_html)

        cfg_path.write_text(json.dumps({"mode": "interactive"}, indent=2) + "\n", encoding="utf-8")
        interactive_resp = general_page(_request("/problems/sample/alice/general"), "sample", "alice")
        self.assertEqual(interactive_resp.status_code, 200)
        interactive_html = interactive_resp.body.decode("utf-8", errors="replace")
        self.assertIn(">Interactor</span>", interactive_html)

    def test_solutions_nav_status_shows_no_main_correct_hint_when_missing_main(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        cfg_path = ws / "config" / "build.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = {}
        if cfg_path.exists() and cfg_path.is_file():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg = dict(raw)
            except Exception:
                cfg = {}
        cfg.pop("accepted_solution_source", None)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

        resp = general_page(_request("/problems/sample/alice/general"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'data-page="solutions"[^>]*>\s*<span class="submenu-title">Solution files</span>\s*<span class="submenu-status submenu-status-danger">\s*1 file \(no main correct\)\s*</span>',
        )

    def test_checker_page_sets_standard_checker_metadata(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = "checkers/checker.cpp"
        (ws / rel).write_text("// checker placeholder\n", encoding="utf-8")

        before = checker_page(_request("/problems/sample/alice/checker"), "sample", "alice")
        self.assertEqual(before.status_code, 200)
        before_html = before.body.decode("utf-8", errors="replace")
        self.assertIn("Standard Checker", before_html)
        self.assertNotIn('aria-label="standard checker help"', before_html)
        self.assertNotIn('aria-label="checker info help"', before_html)
        self.assertIn("checker/set-standard", before_html)
        self.assertIn("name=\"checker_name\"", before_html)
        self.assertIn("std::fcmp.cpp - compare files as sequence of full lines (exact)", before_html)

        install = checker_set_standard(
            problem="sample",
            user="alice",
            checker_name="std::fcmp.cpp",
        )
        self.assertEqual(install.status_code, 303)
        loc = install.headers.get("location", "")
        self.assertIn("/problems/sample/alice/checker", loc)

        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(build_cfg.get("checker_standard"), "std::fcmp.cpp")
        self.assertFalse((ws / rel).exists())

        after = checker_page(_request("/problems/sample/alice/checker"), "sample", "alice")
        self.assertEqual(after.status_code, 200)
        after_html = after.body.decode("utf-8", errors="replace")
        self.assertIn('aria-label="checker details"', after_html)
        self.assertIn('data-tooltip="std::fcmp.cpp - compare files as sequence of full lines (exact)"', after_html)
        self.assertNotIn('class="status-hint-icon"', after_html)
        self.assertNotIn('aria-label="standard checker help"', after_html)

    def test_checker_page_supports_source_save_without_files_page(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = "checkers/checker.cpp"
        (ws / rel).unlink(missing_ok=True)

        resp = checker_page(_request("/problems/sample/alice/checker"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Create checker.cpp template", html)
        self.assertNotIn("checker/save-source", html)
        self.assertNotIn("src=checker", html)

        saved = checker_save_source(
            problem="sample",
            user="alice",
            path=rel,
            content="int main(int argc, char** argv){return argc > 0 ? 0 : 1;}\n",
        )
        self.assertEqual(saved.status_code, 303)
        self.assertIn("return argc", (ws / rel).read_text(encoding="utf-8"))

    def test_checker_save_source_compile_check_failure_keeps_standard_checker_mode(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = "checkers/checker.cpp"
        (ws / rel).unlink(missing_ok=True)

        set_standard = checker_set_standard(problem="sample", user="alice", checker_name="std::fcmp.cpp")
        self.assertEqual(set_standard.status_code, 303)
        cfg_before = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(str(cfg_before.get("checker_standard") or ""), "std::fcmp.cpp")
        self.assertFalse((ws / rel).exists())

        failed = checker_save_source(
            problem="sample",
            user="alice",
            path=rel,
            content="int main( { return 0; }\n",
        )
        self.assertEqual(failed.status_code, 303)
        self.assertEqual(failed.headers.get("location", ""), "/problems/sample/alice/checker")
        messages = _flash_messages_from_response(failed)
        self.assertTrue(messages)
        self.assertIn("compile check failed", messages[0].lower())
        self.assertFalse((ws / rel).exists())

        cfg_after = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(str(cfg_after.get("checker_standard") or ""), "std::fcmp.cpp")
        self.assertNotIn("checker_source", cfg_after)

    def test_generators_page_supports_template_and_source_save(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel_name = "gen.cpp"
        rel_second_name = "other.cpp"
        rel = f"generators/{rel_name}"
        rel_second = f"generators/{rel_second_name}"
        (ws / rel).unlink(missing_ok=True)
        (ws / rel_second).unlink(missing_ok=True)

        page = generators_page(_request("/problems/sample/alice/generators"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Generators", html)
        self.assertIn("Generators List", html)
        self.assertIn("Used in Build", html)
        self.assertIn("Create Generator Template", html)
        self.assertIn("generators/create-template", html)
        self.assertIn('id="generator-create-path"', html)
        self.assertIn('placeholder="generator.cpp"', html)
        self.assertNotIn("generators/save-source", html)

        created = generator_create_template(problem="sample", user="alice", path=rel_name)
        self.assertEqual(created.status_code, 303)
        self.assertTrue((ws / rel).exists())
        created_text = (ws / rel).read_text(encoding="utf-8")
        self.assertIn("registerGen", created_text)
        page_after_create = generators_page(_request("/problems/sample/alice/generators"), "sample", "alice")
        self.assertEqual(page_after_create.status_code, 200)
        self.assertIn("generators/save-source", page_after_create.body.decode("utf-8", errors="replace"))

        cfg_path = ws / "config" / "build.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual([str(x) for x in cfg.get("generator_sources", [])], [rel])

        saved = generator_save_source(
            problem="sample",
            user="alice",
            path=rel,
            content="#include \"testlib.h\"\nint main(int argc,char** argv){registerGen(argc, argv, 1); println(42); return 0;}\n",
        )
        self.assertEqual(saved.status_code, 303)
        self.assertIn("println(42)", (ws / rel).read_text(encoding="utf-8"))

        created_second = generator_create_template(problem="sample", user="alice", path=rel_second_name)
        self.assertEqual(created_second.status_code, 303)
        cfg_after_second = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual([str(x) for x in cfg_after_second.get("generator_sources", [])], [rel, rel_second])
        list_page = generators_page(_request("/problems/sample/alice/generators"), "sample", "alice")
        self.assertEqual(list_page.status_code, 200)
        list_html = list_page.body.decode("utf-8", errors="replace")
        self.assertIn(rel, list_html)
        self.assertIn(rel_second, list_html)

    def test_generator_save_source_compile_check_failure_does_not_persist_source_or_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = "generators/gen_keep.cpp"
        rel_bad = "generators/gen_bad.cpp"
        (ws / rel).unlink(missing_ok=True)
        (ws / rel_bad).unlink(missing_ok=True)

        created = generator_create_template(problem="sample", user="alice", path=rel)
        self.assertEqual(created.status_code, 303)

        ok_saved = generator_save_source(
            problem="sample",
            user="alice",
            path=rel,
            content="#include \"testlib.h\"\nint main(int argc,char** argv){registerGen(argc, argv, 1); println(7); return 0;}\n",
        )
        self.assertEqual(ok_saved.status_code, 303)
        self.assertIn("println(7)", (ws / rel).read_text(encoding="utf-8"))

        cfg_path = ws / "config" / "build.json"
        cfg_before = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual([str(x) for x in cfg_before.get("generator_sources", [])], [rel])

        failed = generator_save_source(
            problem="sample",
            user="alice",
            path=rel_bad,
            content="#include \"testlib.h\"\nint main( { return 0; }\n",
        )
        self.assertEqual(failed.status_code, 303)
        failed_location = failed.headers.get("location", "")
        self.assertIn("/problems/sample/alice/generators", failed_location)
        self.assertIn("path=generators%2Fgen_bad.cpp", failed_location)
        messages = _flash_messages_from_response(failed)
        self.assertTrue(messages)
        self.assertIn("compile check failed", messages[0].lower())
        self.assertFalse((ws / rel_bad).exists())

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual([str(x) for x in cfg_after.get("generator_sources", [])], [rel])

    def test_solutions_page_uses_desc_metadata_for_expected_behavior(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        source_rel = "solutions/std.cpp"
        source = ws / source_rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("int main(){return 0;}\n", encoding="utf-8")
        desc = ws / "solutions/std.cpp.desc"
        desc.write_text("expected: wrong-answer\nnote: baseline negative case\n", encoding="utf-8")

        page = solutions_page(_request("/problems/sample/alice/solutions"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Solution Files", html)
        self.assertIn("solutions/std.cpp", html)
        self.assertIn("wrong_answer (WA)", html)
        self.assertIn("solutions/std.cpp.desc", html)
        self.assertIn("solutions/set-tag", html)
        self.assertIn("class=\"tag-select", html)
        self.assertIn(">accepted (AC)</option>", html)
        self.assertIn(">wrong_answer (WA)</option>", html)
        self.assertNotIn("<th>Tags</th>", html)
        self.assertNotIn("<th>Metadata</th>", html)

        set_tag = solutions_set_tag(
            problem="sample",
            user="alice",
            source_path=source_rel,
            expected_behavior="accepted",
        )
        self.assertEqual(set_tag.status_code, 303)
        self.assertTrue(desc.exists())
        self.assertIn("expected: accepted", desc.read_text(encoding="utf-8"))

        after = general_page(_request("/problems/sample/alice/general"), "sample", "alice")
        self.assertEqual(after.status_code, 200)
        after_html = after.body.decode("utf-8", errors="replace")
        self.assertIn("/checker", after_html)
        self.assertIn("/validator", after_html)
        self.assertIn("/solutions", after_html)
        self.assertIn(">Checker</span>", after_html)
        self.assertIn(">Validator</span>", after_html)
        self.assertIn(">Solution files</span>", after_html)

    def test_solutions_page_supports_template_action(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        target_rel = "solutions/new_wa.cpp"
        (ws / target_rel).unlink(missing_ok=True)
        (ws / f"{target_rel}.desc").unlink(missing_ok=True)

        page = solutions_page(_request("/problems/sample/alice/solutions"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("solutions/create-template", html)
        self.assertIn("name=\"path\"", html)
        self.assertIn('id="solution-create-path"', html)
        self.assertIn('placeholder="accepted.cpp"', html)
        self.assertNotIn("dedicated Solution Editor", html)

        create = solutions_create_template(problem="sample", user="alice", path="new_wa.cpp")
        self.assertEqual(create.status_code, 303)
        location = create.headers.get("location", "")
        self.assertIn("/problems/sample/alice/solutions/editor", location)
        self.assertIn("path=solutions%2Fnew_wa.cpp", location)
        self.assertTrue((ws / target_rel).exists())
        self.assertEqual("", (ws / target_rel).read_text(encoding="utf-8"))
        desc_text = (ws / f"{target_rel}.desc").read_text(encoding="utf-8")
        self.assertIn("expected: wrong_answer", desc_text)

    def test_solutions_template_action_supports_python_path(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        target_rel = "solutions/new_py.py"
        (ws / target_rel).unlink(missing_ok=True)
        (ws / f"{target_rel}.desc").unlink(missing_ok=True)

        create = solutions_create_template(problem="sample", user="alice", path="new_py.py")
        self.assertEqual(create.status_code, 303)
        location = create.headers.get("location", "")
        self.assertIn("/problems/sample/alice/solutions/editor", location)
        self.assertIn("path=solutions%2Fnew_py.py", location)
        self.assertTrue((ws / target_rel).exists())
        self.assertEqual("", (ws / target_rel).read_text(encoding="utf-8"))
        desc_text = (ws / f"{target_rel}.desc").read_text(encoding="utf-8")
        self.assertIn("expected: unknown", desc_text)

    def test_solutions_page_includes_rename_and_delete_actions(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        token = uuid.uuid4().hex[:8]
        source_rel = f"solutions/action_{token}.cpp"
        source_abs = ws / source_rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        source_abs.write_text("int main(){return 0;}\n", encoding="utf-8")

        page = solutions_page(_request("/problems/sample/alice/solutions"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("solutions/rename", html)
        self.assertIn("solutions/delete", html)
        self.assertIn("name=\"old_path\"", html)
        self.assertIn("name=\"new_path\"", html)
        self.assertIn(f'name="new_path" value="{Path(source_rel).name}"', html)
        self.assertIn("Delete", html)
        self.assertIn(source_rel, html)

    def test_solutions_set_tag_main_correct_updates_main_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        token = uuid.uuid4().hex[:8]
        main_rel = f"solutions/main_{token}.cpp"
        other_rel = f"solutions/other_{token}.cpp"
        (ws / main_rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / other_rel).write_text("int main(){return 1;}\n", encoding="utf-8")
        cfg_path = ws / "config" / "build.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{}\n", encoding="utf-8")

        before = solutions_page(_request("/problems/sample/alice/solutions"), "sample", "alice")
        self.assertEqual(before.status_code, 200)
        before_html = before.body.decode("utf-8", errors="replace")
        self.assertIn("solutions/set-tag", before_html)
        self.assertIn("Main correct solution is required.", before_html)

        resp = solutions_set_tag(
            problem="sample",
            user="alice",
            source_path=main_rel,
            expected_behavior="main_correct",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"path={quote_plus(main_rel)}", resp.headers.get("location", ""))

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(str(cfg_after.get("accepted_solution_source") or ""), main_rel)

        after = solutions_page(_request("/problems/sample/alice/solutions"), "sample", "alice")
        self.assertEqual(after.status_code, 200)
        after_html = after.body.decode("utf-8", errors="replace")
        self.assertNotIn("Main correct solution is required.", after_html)
        self.assertRegex(
            after_html,
            rf'<input type="hidden" name="source_path" value="{re.escape(main_rel)}"\s*/?>[\s\S]*?<option value="main_correct" selected>',
        )

    def test_solutions_set_tag_accepted_does_not_mark_main_correct(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        token = uuid.uuid4().hex[:8]
        source_rel = f"solutions/ac_{token}.cpp"
        (ws / source_rel).write_text("int main(){return 0;}\n", encoding="utf-8")
        cfg_path = ws / "config" / "build.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{}\n", encoding="utf-8")

        resp = solutions_set_tag(
            problem="sample",
            user="alice",
            source_path=source_rel,
            expected_behavior="accepted",
        )
        self.assertEqual(resp.status_code, 303)

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("accepted_solution_source", cfg_after)

        page = solutions_page(_request("/problems/sample/alice/solutions"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            rf'<input type="hidden" name="source_path" value="{re.escape(source_rel)}"\s*/?>[\s\S]*?<option value="accepted" selected>',
        )

    def test_solutions_rename_moves_desc_and_updates_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        token = uuid.uuid4().hex[:8]
        old_rel = f"solutions/rename_old_{token}.cpp"
        new_rel = f"solutions/rename_new_{token}.cpp"
        old_abs = ws / old_rel
        new_abs = ws / new_rel
        old_abs.parent.mkdir(parents=True, exist_ok=True)
        old_abs.write_text("int main(){return 1;}\n", encoding="utf-8")
        old_desc_abs = ws / f"{old_rel}.desc"
        old_desc_abs.write_text("expected: wrong_answer\nnote: keep me\n", encoding="utf-8")
        new_abs.unlink(missing_ok=True)
        (ws / f"{new_rel}.desc").unlink(missing_ok=True)

        cfg_path = ws / "config" / "build.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"accepted_solution_source": old_rel}, indent=2) + "\n", encoding="utf-8")

        resp = solutions_rename(problem="sample", user="alice", old_path=old_rel, new_path=Path(new_rel).name)
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/solutions", location)
        self.assertIn(f"path={quote_plus(new_rel)}", location)

        self.assertFalse(old_abs.exists())
        self.assertTrue(new_abs.exists())
        self.assertFalse(old_desc_abs.exists())
        new_desc_abs = ws / f"{new_rel}.desc"
        self.assertTrue(new_desc_abs.exists())
        self.assertIn("expected: wrong_answer", new_desc_abs.read_text(encoding="utf-8"))
        self.assertIn("note: keep me", new_desc_abs.read_text(encoding="utf-8"))

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(str(cfg_after.get("accepted_solution_source") or ""), new_rel)

    def test_solutions_delete_removes_source_desc_and_clears_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        token = uuid.uuid4().hex[:8]
        source_rel = f"solutions/delete_{token}.cpp"
        source_abs = ws / source_rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        source_abs.write_text("int main(){return 2;}\n", encoding="utf-8")
        desc_abs = ws / f"{source_rel}.desc"
        desc_abs.write_text("expected: accepted\n", encoding="utf-8")

        cfg_path = ws / "config" / "build.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"accepted_solution_source": source_rel}, indent=2) + "\n", encoding="utf-8")

        resp = solutions_delete(problem="sample", user="alice", source_path=source_rel)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/sample/alice/solutions", resp.headers.get("location", ""))
        self.assertFalse(source_abs.exists())
        self.assertFalse(desc_abs.exists())

        cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("accepted_solution_source", cfg_after)

    def test_solutions_editor_page_saves_source_without_files_page(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        target_rel = "solutions/editor_case.cpp"
        (ws / target_rel).unlink(missing_ok=True)
        (ws / f"{target_rel}.desc").unlink(missing_ok=True)

        created = solutions_create_template(problem="sample", user="alice", path=target_rel)
        self.assertEqual(created.status_code, 303)
        self.assertIn("/solutions/editor", created.headers.get("location", ""))

        editor = solutions_editor_page(
            _request("/problems/sample/alice/solutions/editor", f"path={target_rel}"),
            "sample",
            "alice",
        )
        self.assertEqual(editor.status_code, 200)
        editor_html = editor.body.decode("utf-8", errors="replace")
        self.assertIn("Solution Editor", editor_html)
        self.assertIn("solutions/save-source", editor_html)
        self.assertIn("solution-save-form", editor_html)
        self.assertIn("solution-save-submit", editor_html)
        self.assertIn("solution-save-error", editor_html)
        self.assertIn("solution-expected-behavior", editor_html)
        self.assertNotIn("page-grid single-column", editor_html)
        self.assertIn('class="side-panel"', editor_html)
        self.assertNotIn("<h2>Current Source</h2>", editor_html)
        self.assertNotIn("<h2>Solutions</h2>", editor_html)
        self.assertNotIn("src=solutions", editor_html)

        updated_content = "int main(){return 42;}\n"
        saved = solutions_save_source(
            _request("/problems/sample/alice/solutions/save-source", method="POST"),
            problem="sample",
            user="alice",
            source_path=target_rel,
            content=updated_content,
            expected_behavior="wrong_answer",
        )
        self.assertEqual(saved.status_code, 303)
        saved_location = saved.headers.get("location", "")
        self.assertIn("/solutions/editor", saved_location)
        self.assertIn("path=solutions%2Feditor_case.cpp", saved_location)
        self.assertEqual((ws / target_rel).read_text(encoding="utf-8"), updated_content)
        self.assertIn("expected: wrong_answer", (ws / f"{target_rel}.desc").read_text(encoding="utf-8"))

        toast_page = solutions_editor_page(
            _request_with_cookie(
                "/problems/sample/alice/solutions/editor",
                _flash_cookie_header("solution source saved"),
                "path=solutions/editor_case.cpp",
            ),
            "sample",
            "alice",
        )
        toast_html = toast_page.body.decode("utf-8", errors="replace")
        self.assertIn('<p class="flash flash-floating-center" data-autohide="1">solution source saved</p>', toast_html)

    def test_solutions_save_source_rejects_compile_error_and_keeps_previous_content(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        target_rel = "solutions/editor_ce_case.cpp"
        source_abs = ws / target_rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        original_content = "int main(){return 0;}\n"
        source_abs.write_text(original_content, encoding="utf-8")

        resp = solutions_save_source(
            _request("/problems/sample/alice/solutions/save-source", method="POST"),
            problem="sample",
            user="alice",
            source_path=target_rel,
            content="int main(){\n",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location", ""), "/problems/sample/alice/solutions/editor?path=solutions%2Feditor_ce_case.cpp")
        self.assertEqual(source_abs.read_text(encoding="utf-8"), original_content)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        message = messages[0].lower()
        self.assertTrue(("error" in message) or ("compile" in message) or ("syntax" in message))

    def test_solutions_save_source_ajax_returns_error_without_persisting_ce_content(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        target_rel = "solutions/editor_ajax_ce_case.cpp"
        source_abs = ws / target_rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        original_content = "int main(){return 0;}\n"
        source_abs.write_text(original_content, encoding="utf-8")

        req = _request(
            "/problems/sample/alice/solutions/save-source",
            method="POST",
            headers=[(b"x-requested-with", b"fetch"), (b"accept", b"application/json")],
        )
        resp = solutions_save_source(
            req,
            problem="sample",
            user="alice",
            source_path=target_rel,
            content="int main(){\n",
        )
        self.assertEqual(resp.status_code, 400)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertFalse(bool(payload.get("ok")))
        self.assertTrue(str(payload.get("error") or "").strip())
        self.assertEqual(source_abs.read_text(encoding="utf-8"), original_content)

    def test_solutions_save_source_ajax_success_returns_redirect(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        target_rel = "solutions/editor_ajax_ok_case.cpp"
        source_abs = ws / target_rel
        source_abs.parent.mkdir(parents=True, exist_ok=True)
        source_abs.write_text("int main(){return 0;}\n", encoding="utf-8")

        req = _request(
            "/problems/sample/alice/solutions/save-source",
            method="POST",
            headers=[(b"x-requested-with", b"fetch"), (b"accept", b"application/json")],
        )
        updated_content = "int main(){return 7;}\n"
        resp = solutions_save_source(
            req,
            problem="sample",
            user="alice",
            source_path=target_rel,
            content=updated_content,
            expected_behavior="accepted",
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(
            str(payload.get("redirect") or ""),
            "/problems/sample/alice/solutions/editor?path=solutions%2Feditor_ajax_ok_case.cpp",
        )
        self.assertEqual(source_abs.read_text(encoding="utf-8"), updated_content)
        self.assertIn("expected: accepted", (ws / f"{target_rel}.desc").read_text(encoding="utf-8"))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("solution source", messages[0])

    def test_checker_view_standard_page_shows_source(self) -> None:
        resp = checker_view_standard(
            _request("/problems/sample/alice/checker/view-standard", "checker_name=std%3A%3Afcmp.cpp"),
            "sample",
            "alice",
            checker_name="std::fcmp.cpp",
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("View Standard Checker", html)
        self.assertIn("std::fcmp.cpp", html)
        self.assertIn("third_party/upstream/testlib/checkers/", html)
        self.assertIn("registerTestlibCmd", html)
        self.assertIn("Use This Standard Checker", html)

    def test_validator_and_interactor_pages_support_template_actions(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        validator_rel = "validators/validator.cpp"
        interactor_rel = "interactors/interactor.cpp"
        (ws / validator_rel).unlink(missing_ok=True)
        (ws / interactor_rel).unlink(missing_ok=True)

        validator_resp = validator_page(_request("/problems/sample/alice/validator"), "sample", "alice")
        self.assertEqual(validator_resp.status_code, 200)
        validator_html = validator_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Validator", validator_html)
        self.assertIn("validator/create-template", validator_html)
        self.assertNotIn("validator/save-source", validator_html)
        self.assertNotIn("src=validator", validator_html)

        interactor_resp = interactor_page(_request("/problems/sample/alice/interactor"), "sample", "alice")
        self.assertEqual(interactor_resp.status_code, 200)
        interactor_html = interactor_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Interactor", interactor_html)
        self.assertIn("interactor/create-template", interactor_html)
        self.assertNotIn("interactor/save-source", interactor_html)
        self.assertNotIn("src=interactor", interactor_html)

        validator_create = validator_create_template(problem="sample", user="alice", path=validator_rel)
        self.assertEqual(validator_create.status_code, 303)
        self.assertTrue((ws / validator_rel).exists())
        self.assertIn("registerValidation", (ws / validator_rel).read_text(encoding="utf-8"))
        validator_after_create = validator_page(_request("/problems/sample/alice/validator"), "sample", "alice")
        validator_after_html = validator_after_create.body.decode("utf-8", errors="replace")
        self.assertIn("validator/save-source", validator_after_html)
        self.assertIn("Save Validator Source", validator_after_html)
        self.assertIn("Create Validator Template", validator_after_html)
        self.assertIn("formaction=\"/problems/sample/alice/validator/create-template\"", validator_after_html)

        interactor_create = interactor_create_template(problem="sample", user="alice", path=interactor_rel)
        self.assertEqual(interactor_create.status_code, 303)
        self.assertTrue((ws / interactor_rel).exists())
        self.assertIn("registerInteraction", (ws / interactor_rel).read_text(encoding="utf-8"))
        interactor_after_create = interactor_page(_request("/problems/sample/alice/interactor"), "sample", "alice")
        interactor_after_html = interactor_after_create.body.decode("utf-8", errors="replace")
        self.assertIn("interactor/save-source", interactor_after_html)

        validator_save = validator_save_source(
            problem="sample",
            user="alice",
            path=validator_rel,
            content="int main(int argc, char** argv){return argc > 0 ? 0 : 1;}\n",
        )
        self.assertEqual(validator_save.status_code, 303)
        self.assertIn("return argc", (ws / validator_rel).read_text(encoding="utf-8"))

        interactor_save = interactor_save_source(
            problem="sample",
            user="alice",
            path=interactor_rel,
            content="int main(int argc, char** argv){return argv != nullptr ? 0 : 1;}\n",
        )
        self.assertEqual(interactor_save.status_code, 303)
        self.assertIn("argv !=", (ws / interactor_rel).read_text(encoding="utf-8"))
