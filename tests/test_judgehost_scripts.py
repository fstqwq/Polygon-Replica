from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.runtime_value import build_runtime_values
from app.service.judgehost.toolkit import DomjudgeToolkit


config = SimpleNamespace(constants=build_runtime_values(), judgehost_task_service=None)


class TestJudgehostScripts(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.TemporaryDirectory(prefix="judgehost-scripts-")
        self.addCleanup(self._root.cleanup)
        config.constants = build_runtime_values()
        state = SimpleNamespace(
            constants=config.constants,
        )
        config.judgehost_task_service = SimpleNamespace(toolkit=DomjudgeToolkit(state))

    def test_domjudge_compare_script_shifts_framework_args_before_checker(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script().decode("utf-8")
        self.assertIn("shift 3", script_text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "echo \"argc:$#\"\n"
                "if [ \"$#\" -eq 4 ]; then\n"
                "  exit 42\n"
                "fi\n"
                "exit 3\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ok\n", encoding="utf-8")
            test_ans.write_text("ok\n", encoding="utf-8")
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback), "--flag"],
                input="ok\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 42)
            checker_log = (feedback / "checker.log").read_text(encoding="utf-8", errors="replace")
            self.assertIn("argc:4", checker_log)

    def test_domjudge_compare_script_uses_testlib_arg_convention_with_stdin_team_output(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script().decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "if [ \"$#\" -lt 3 ]; then\n"
                "  echo \"bad argc:$#\"\n"
                "  exit 3\n"
                "fi\n"
                "case \"$2\" in\n"
                "  */001.ans) ;;\n"
                "  *)\n"
                "    echo \"bad answer arg\"\n"
                "    exit 3\n"
                "    ;;\n"
                "esac\n"
                "case \"$3\" in\n"
                "  */feedback) ;;\n"
                "  *)\n"
                "    echo \"bad report output arg\"\n"
                "    exit 3\n"
                "    ;;\n"
                "esac\n"
                "read token || { echo \"Unexpected end of file - double expected\"; exit 43; }\n"
                "if [ \"$token\" = \"20\" ]; then\n"
                "  echo \"ok\"\n"
                "  exit 42\n"
                "fi\n"
                "echo \"wrong answer\"\n"
                "exit 43\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("ignored\n", encoding="utf-8")
            feedback.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 42)
            checker_log = (feedback / "checker.log").read_text(encoding="utf-8", errors="replace")
            self.assertIn("ok", checker_log)

    def test_domjudge_compare_script_preserves_checker_fail_exit_code(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script().decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "echo \"FAIL Can not write to the result file (test case 1)\"\n"
                "exit 3\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("ignored\n", encoding="utf-8")
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("FAIL Can not write to the result file", result.stderr)
            judge_message = (feedback / "judgemessage.txt").read_text(encoding="utf-8", errors="replace")
            self.assertIn("FAIL Can not write to the result file", judge_message)

    def test_domjudge_compare_script_preserves_existing_judgemessage_on_checker_fail(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script().decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "mkdir -p \"$3\"\n"
                "echo \"FAIL checker detailed message\" >\"$3/judgemessage.txt\"\n"
                "exit 3\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("ignored\n", encoding="utf-8")
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("FAIL checker detailed message", result.stderr)
            judge_message = (feedback / "judgemessage.txt").read_text(encoding="utf-8", errors="replace")
            self.assertIn("FAIL checker detailed message", judge_message)

    def test_domjudge_compare_script_in_main_correct_mode_uses_self_answer(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(main_correct=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            feedback.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 42)

    def test_domjudge_compare_script_in_main_correct_mode_runs_checker(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(main_correct=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            checker = root / "checker"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            run_script.write_text(script_text, encoding="utf-8")
            checker.write_text(
                "#!/bin/sh\n"
                "ans=$(cat \"$2\")\n"
                "out=$(cat)\n"
                "[ \"$ans\" = \"$out\" ] || exit 43\n"
                "printf 'checker ok\\n' >\"$3/judgemessage.txt\"\n"
                "exit 42\n",
                encoding="utf-8",
            )
            os.chmod(run_script, 0o755)
            os.chmod(checker, 0o755)
            test_in.write_text("ignored\n", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            feedback.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(run_script), str(test_in), str(test_ans), str(feedback)],
                input="20\n",
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 42, result.stderr)
            judge_message = (feedback / "judgemessage.txt").read_text(encoding="utf-8", errors="replace")
            self.assertIn("checker ok", judge_message)

    def test_domjudge_compile_script_uses_configurable_flags(self) -> None:
        service = config.judgehost_task_service
        old_values = config.constants.to_dict()
        self.addCleanup(config.constants.replace, old_values)
        patched = dict(old_values)
        patched["TOOLCHAIN_CPP_COMPILER"] = "clang++"
        patched["TOOLCHAIN_JAVA_COMPILER"] = "javac-custom"
        patched["TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS"] = "-O3 -std=gnu++20 -DNDEBUG"
        patched["TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS"] = "--release 17 -encoding UTF-8"
        patched["TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS"] = "-X dev"
        config.constants.replace(patched)

        cpp_script = service.toolkit.compile_script("submission.cpp").decode("utf-8")
        java_script = service.toolkit.compile_script("submission.java").decode("utf-8")
        py_script = service.toolkit.compile_script("submission.py").decode("utf-8")
        self.assertIn('exec clang++ -O3 -std=gnu++20 -DNDEBUG -I. "$MAIN" -o "$DEST"', cpp_script)
        self.assertIn("javac-custom --release 17", java_script)
        self.assertIn('-sourcepath . -d . "$@"', java_script)
        self.assertIn('"$PY" -X dev -m py_compile "$MAIN"', py_script)

    def test_domjudge_java_compile_script_uses_detect_main_contract(self) -> None:
        service = config.judgehost_task_service
        java_script = service.toolkit.compile_script("submission.java").decode("utf-8")
        java_compile_only_script = service.toolkit.compile_script(
            "submission.java",
            compile_only=True,
        ).decode("utf-8")
        self.assertIn("trying to detect main class", java_script)
        self.assertIn('DetectMain.java', java_script)
        self.assertIn('java -cp "$COMPILESCRIPTDIR" DetectMain', java_script)
        self.assertIn("trying to detect main class", java_compile_only_script)
        self.assertIn('DetectMain.java', java_compile_only_script)

    def test_domjudge_python_compile_script_works_without_entry_point_env(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compile_script("submission.py").decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "run"
            source = root / "submission.py"
            dest = root / "program"
            source.write_text("print('ok')\n", encoding="utf-8")
            script.write_text(script_text, encoding="utf-8")
            os.chmod(script, 0o755)
            env = dict(os.environ)
            env.pop("ENTRY_POINT", None)
            result = subprocess.run(
                [str(script), str(dest), "262144", str(source)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(dest.exists())
            launcher = dest.read_text(encoding="utf-8", errors="replace")
            self.assertIn("exec ", launcher)
            self.assertIn("submission.py", launcher)

    def test_domjudge_interactive_run_script_uses_official_runpipe_wrapper(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(True, main_correct=False).decode("utf-8")
        self.assertIn("runpipe", script_text)
        self.assertIn("runjury", script_text)
        self.assertIn("TESTOUT", script_text)
        self.assertIn("META", script_text)
        self.assertNotIn("INTERACTOR_BIN", script_text)

    def test_domjudge_cpp_executable_build_script_comes_from_asset(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.cpp_executable_build_script(
            "interactor.cpp",
            role="interactor",
        ).decode("utf-8")
        self.assertIn("#!/bin/sh", script_text)
        self.assertIn("Auto-generated build script for interactor by Polygon2DOMjudge", script_text)
        self.assertIn("g++ -Wall -DDOMJUDGE -O2 interactor.cpp -std=gnu++20 -o run", script_text)

    def test_domjudge_generate_run_script_executes_submission_runner_with_payload_args(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(
            False,
            main_correct=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            submission_runner = root / "program"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\" 7\n", encoding="utf-8")
            submission_runner.write_text("#!/bin/sh\nprintf 'runner:%s\\n' \"$1\"\n", encoding="utf-8")
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "runner:7\n")

    def test_domjudge_generate_run_script_handles_option_like_payload_args(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(
            False,
            main_correct=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            submission_runner = root / "program"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\" -n\n", encoding="utf-8")
            submission_runner.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n", encoding="utf-8")
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "-n\n")

    def test_domjudge_generate_run_script_preserves_wrapper_command_vector_when_appending_payload(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(
            False,
            main_correct=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            wrapper = root / "wrapper"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\" 7 8\n", encoding="utf-8")
            wrapper.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "for arg in \"$@\"; do\n"
                "  printf '%s\\n' \"$arg\"\n"
                "done\n",
                encoding="utf-8",
            )
            os.chmod(wrapper, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(wrapper), "A", "B", "C"],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                prog_out.read_text(encoding="utf-8").splitlines(),
                ["A", "B", "C", "7", "8"],
            )

    def test_domjudge_generate_run_script_supports_plain_argument_payload(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(
            False,
            main_correct=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            submission_runner = root / "program"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("9\n", encoding="utf-8")
            submission_runner.write_text("#!/bin/sh\nprintf 'plain:%s\\n' \"$1\"\n", encoding="utf-8")
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "plain:9\n")

    def test_domjudge_generate_run_script_accepts_submission_bin_only_payload(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(
            False,
            main_correct=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            case_dir = root / "case"
            test_in = case_dir / "001.in"
            prog_out = case_dir / "program.out"
            submission_runner = root / "program"
            unrelated_cwd = root / "other-cwd"
            case_dir.mkdir(parents=True, exist_ok=True)
            unrelated_cwd.mkdir(parents=True, exist_ok=True)
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("\"$SUBMISSION_BIN\"\n", encoding="utf-8")
            submission_runner.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "if [ \"$#\" -eq 0 ]; then\n"
                "  printf 'no-extra-args\\n'\n"
                "  exit 0\n"
                "fi\n"
                "printf 'unexpected:%s\\n' \"$1\"\n",
                encoding="utf-8",
            )
            os.chmod(submission_runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(submission_runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=unrelated_cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "no-extra-args\n")

    def test_domjudge_generate_run_script_marks_nondeterministic_output(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(
            False,
            main_correct=False,
            compile_only=False,
            generate_mode=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            state = root / "counter"
            runner = root / "program"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text('"$SUBMISSION_BIN"\n', encoding="utf-8")
            runner.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "state=counter\n"
                "count=0\n"
                "if [ -f \"$state\" ]; then count=$(cat \"$state\"); fi\n"
                "count=$((count + 1))\n"
                "printf '%s' \"$count\" >\"$state\"\n"
                "printf '%s\\n' \"$count\"\n",
                encoding="utf-8",
            )
            os.chmod(runner, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(runner)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "1\n")
            self.assertEqual(state.read_text(encoding="utf-8"), "2")
            self.assertTrue((root / "program.out.repeatability-failed").exists())
            self.assertFalse(list(root.glob("program.out.repeat.[0-9]*")))

    def test_domjudge_generate_compare_script_rejects_repeatability_marker(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compare_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            team_out = root / "program.out"
            validator = root / "validator"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            test_in.write_text("", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            team_out.write_text("42\n", encoding="utf-8")
            (root / "program.out.repeatability-failed").write_text("", encoding="utf-8")
            validator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(validator, 0o755)
            result = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 43, result.stderr)
            self.assertEqual(
                (feedback / "judgemessage.txt").read_text(encoding="utf-8"),
                "generator output differs between two runs\n",
            )

    def test_domjudge_generate_compare_script_runs_validator(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compare_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            team_out = root / "program.out"
            validator = root / "validator"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            test_in.write_text("", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            team_out.write_text("42\n", encoding="utf-8")
            validator.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "read -r token || exit 1\n"
                "[ \"$token\" = \"42\" ] || exit 1\n",
                encoding="utf-8",
            )
            os.chmod(validator, 0o755)

            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)

            team_out.write_text("41\n", encoding="utf-8")
            bad = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(bad.returncode, 43, bad.stderr)

    def test_domjudge_generate_compare_script_writes_testlib_overview_log(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compare_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            team_out = root / "program.out"
            validator = root / "validator"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            test_in.write_text("", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            team_out.write_text("42\n", encoding="utf-8")
            validator.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "overview=''\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"--testOverviewLogFileName\" ]; then\n"
                "    shift\n"
                "    overview=\"$1\"\n"
                "  fi\n"
                "  shift || true\n"
                "done\n"
                "read -r token || exit 1\n"
                "[ \"$token\" = \"42\" ] || exit 1\n"
                "[ -n \"$overview\" ] || exit 1\n"
                "printf '\"n\": min-value-hit\\nconstant-bounds \"n\": 1 3\\nvariable \"n\"\\n' >\"$overview\"\n",
                encoding="utf-8",
            )
            os.chmod(validator, 0o755)

            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)
            judge_message = (feedback / "judgemessage.txt").read_text(encoding="utf-8", errors="replace")
            self.assertEqual(judge_message, '"n": min-value-hit\nconstant-bounds "n": 1 3\nvariable "n"\n')

    def test_domjudge_generate_compare_script_prefers_feedback_program_out_over_stdin(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compare_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            feedback.mkdir(parents=True, exist_ok=True)
            (feedback / "program.out").write_text("42\n", encoding="utf-8")
            test_in.write_text("\"$SUBMISSION_BIN\"\n", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            validator = root / "validator"
            validator.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "read -r token || exit 1\n"
                "[ \"$token\" = \"42\" ] || exit 1\n",
                encoding="utf-8",
            )
            os.chmod(validator, 0o755)

            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback)],
                input="\"$SUBMISSION_BIN\"\n",
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)

    def test_domjudge_generate_compare_script_prefers_cwd_program_out_over_stdin(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compare_script = root / "run"
            test_in = root / "001.in"
            test_ans = root / "001.ans"
            feedback = root / "feedback"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            feedback.mkdir(parents=True, exist_ok=True)
            (root / "program.out").write_text("42\n", encoding="utf-8")
            test_in.write_text("\"$SUBMISSION_BIN\"\n", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            validator = root / "validator"
            validator.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "read -r token || exit 1\n"
                "[ \"$token\" = \"42\" ] || exit 1\n",
                encoding="utf-8",
            )
            os.chmod(validator, 0o755)

            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback)],
                input="\"$SUBMISSION_BIN\"\n",
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)

    def test_domjudge_generate_compare_script_prefers_program_out_next_to_feedback_over_stdin(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            work_dir = root / "work"
            feedback = work_dir / "feedback"
            compare_script = scripts_dir / "run"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            feedback.mkdir(parents=True, exist_ok=True)
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            (work_dir / "program.out").write_text("42\n", encoding="utf-8")
            test_in = work_dir / "001.in"
            test_ans = work_dir / "001.ans"
            test_in.write_text("\"$SUBMISSION_BIN\"\n", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            validator = scripts_dir / "validator"
            validator.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "read -r token || exit 1\n"
                "[ \"$token\" = \"42\" ] || exit 1\n",
                encoding="utf-8",
            )
            os.chmod(validator, 0o755)

            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback)],
                input="\"$SUBMISSION_BIN\"\n",
                text=True,
                capture_output=True,
                check=False,
                cwd=scripts_dir,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)

    def test_domjudge_generate_compare_script_compiles_validator_from_readonly_script_dir(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compare_script(generate_mode=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            work_dir = root / "work"
            feedback = work_dir / "feedback"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            compare_script = scripts_dir / "run"
            validator_src = scripts_dir / "validator.cpp"
            test_in = work_dir / "001.in"
            test_ans = work_dir / "001.ans"
            team_out = work_dir / "program.out"
            compare_script.write_text(script_text, encoding="utf-8")
            os.chmod(compare_script, 0o755)
            validator_src.write_text(
                "#include <cstdio>\n"
                "int main(){ long long x=0; if(std::scanf(\"%lld\", &x)!=1) return 1; return x==42 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            test_in.write_text("", encoding="utf-8")
            test_ans.write_text("", encoding="utf-8")
            team_out.write_text("42\n", encoding="utf-8")
            os.chmod(scripts_dir, 0o555)
            ok = subprocess.run(
                [str(compare_script), str(test_in), str(test_ans), str(feedback), str(team_out)],
                text=True,
                capture_output=True,
                check=False,
                cwd=work_dir,
            )
            self.assertEqual(ok.returncode, 42, ok.stderr)

    def test_domjudge_run_script_compile_only_branch_uses_skip_run_copy(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(False, main_correct=False, compile_only=True).decode("utf-8")
        self.assertIn('cat "$TESTIN" >"$PROGOUT"', script_text)
        self.assertIn('"$@" </dev/null >/dev/null', script_text)

    def test_domjudge_run_script_manual_validate_branch_copies_input_to_output(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.run_script(
            False,
            main_correct=False,
            compile_only=False,
            manual_validate_only=True,
        ).decode("utf-8")
        self.assertIn('cat "$TESTIN" >"$PROGOUT"', script_text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_script = root / "run-wrapper"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            noop = root / "noop.sh"
            run_script.write_text(script_text, encoding="utf-8")
            os.chmod(run_script, 0o755)
            test_in.write_text("manual input\n", encoding="utf-8")
            noop.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(noop, 0o755)
            result = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(noop)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "manual input\n")

    def test_domjudge_compile_script_matches_official_wrapper_shape(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compile_script("submission.cpp").decode("utf-8")
        self.assertIn('exec g++ -x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE -I. "$MAIN" -o "$DEST"', script_text)

    def test_domjudge_compile_only_cpp_script_compiles_then_writes_noop_program(self) -> None:
        service = config.judgehost_task_service
        compile_text = service.toolkit.compile_script("submission.cpp", compile_only=True).decode("utf-8")
        run_text = service.toolkit.run_script(False, main_correct=False, compile_only=True).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compile_script = root / "compile-wrapper"
            run_script = root / "run-wrapper"
            dest = root / "program"
            source = root / "submission.cpp"
            test_in = root / "001.in"
            prog_out = root / "program.out"
            compile_script.write_text(compile_text, encoding="utf-8")
            run_script.write_text(run_text, encoding="utf-8")
            os.chmod(compile_script, 0o755)
            os.chmod(run_script, 0o755)
            source.write_text(
                "#include <iostream>\n"
                "int main(){ int *p=nullptr; std::cout << *p << '\\n'; return 0; }\n",
                encoding="utf-8",
            )
            test_in.write_text("compile-only\n", encoding="utf-8")
            compiled = subprocess.run(
                [str(compile_script), str(dest), "65536", str(source)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertTrue(dest.exists())
            self.assertTrue(os.access(dest, os.X_OK))
            self.assertEqual(dest.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")
            executed = subprocess.run(
                [str(run_script), str(test_in), str(prog_out), str(dest)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(prog_out.read_text(encoding="utf-8"), "compile-only\n")

    def test_domjudge_skip_compile_creates_noop_executable(self) -> None:
        service = config.judgehost_task_service
        script_text = service.toolkit.compile_script(
            "manual_validate.cpp",
            manual_validate_only=True,
        ).decode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compile_script = root / "run"
            dest = root / "program"
            source = root / "manual_validate.cpp"
            compile_script.write_text(script_text, encoding="utf-8")
            os.chmod(compile_script, 0o755)
            source.write_text("int main(){return 0;}\n", encoding="utf-8")
            result = subprocess.run(
                [str(compile_script), str(dest), "65536", str(source)],
                text=True,
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(dest.exists())
            self.assertTrue(os.access(dest, os.X_OK))
