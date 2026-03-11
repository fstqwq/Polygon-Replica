from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable

from app.service.sandbox.base import ExecSpec, SandboxBackend


def run_interactive_case(
    *,
    sandbox: SandboxBackend,
    read_time_metrics: Callable[[Path], tuple[int, int]],
    validator_style_verdict: Callable[[int], str],
    cap_tle_time_ms: Callable[[int, int], int],
    interactor_bin: Path,
    submission_bin: Path,
    test: Path,
    ans: Path,
    interactor_output: Path,
    transcript: Path,
    feedback_dir: Path,
    timeout_sec: int = 30,
    timeout_ms: int = 0,
    memory_mb: int = 1024,
    process_limit: int = 64,
    output_kb: int = 65536,
) -> tuple[str, int, int, int]:
    sub_err_path = transcript.with_name(f"{transcript.stem}.submission.stderr.txt")
    itr_err_path = transcript.with_name(f"{transcript.stem}.interactor.stderr.txt")
    sub_cwd = feedback_dir / "submission"
    itr_cwd = feedback_dir / "interactor"
    sub_cwd.mkdir(parents=True, exist_ok=True)
    itr_cwd.mkdir(parents=True, exist_ok=True)
    sub_time_path = sub_cwd / "submission.time.txt"
    itr_input = itr_cwd / "input.in"
    itr_answer = itr_cwd / "answer.ans"
    itr_output = itr_cwd / "output.ans"
    shutil.copy2(test, itr_input)
    shutil.copy2(ans, itr_answer)
    itr_output.unlink(missing_ok=True)
    interactor_output.unlink(missing_ok=True)
    sub_time_path.unlink(missing_ok=True)
    if Path("/usr/bin/time").exists():
        sub_cmd = ["/usr/bin/time", "-f", "%U %S", "-o", sub_time_path.name, str(submission_bin)]
    else:
        sub_cmd = [str(submission_bin)]
    sub_spec = ExecSpec(
        command=sub_cmd,
        cwd=sub_cwd,
        timeout_sec=timeout_sec,
        memory_mb=memory_mb,
        process_limit=process_limit,
        output_kb=output_kb,
    )
    itr_spec = ExecSpec(
        command=[str(interactor_bin), itr_input.name, itr_output.name, itr_answer.name],
        cwd=itr_cwd,
        timeout_sec=timeout_sec,
        memory_mb=memory_mb,
        process_limit=process_limit,
        output_kb=output_kb,
    )
    with sub_err_path.open("wb") as sub_err_fh, itr_err_path.open("wb") as itr_err_fh:
        itr_to_sub_r = -1
        itr_to_sub_w = -1
        sub_to_itr_r = -1
        sub_to_itr_w = -1
        sub: subprocess.Popen[bytes] | None = None
        itr: subprocess.Popen[bytes] | None = None
        itr_env = dict(os.environ)
        itr_env["FEEDBACK_DIR"] = str(itr_cwd)
        try:
            itr_to_sub_r, itr_to_sub_w = os.pipe()
            sub_to_itr_r, sub_to_itr_w = os.pipe()
            sub = sandbox.popen(
                sub_spec,
                stdin=itr_to_sub_r,
                stdout=sub_to_itr_w,
                stderr=sub_err_fh,
                text=False,
                bufsize=0,
            )
            itr = sandbox.popen(
                itr_spec,
                stdin=sub_to_itr_r,
                stdout=itr_to_sub_w,
                stderr=itr_err_fh,
                text=False,
                bufsize=0,
                env=itr_env,
            )
        finally:
            for fd in (itr_to_sub_r, itr_to_sub_w, sub_to_itr_r, sub_to_itr_w):
                if fd < 0:
                    continue
                try:
                    os.close(fd)
                except OSError:
                    pass

        with transcript.open("w", encoding="utf-8") as tf:
            tf.write("interactive bridge: direct pipe\n")
            if sub is None or itr is None:
                tf.write("process setup failed\n")
                return "FL", 0, 0, 0

            start = time.monotonic()
            timed_out = False
            try:
                while True:
                    if time.monotonic() - start > timeout_sec:
                        timed_out = True
                        if sub.poll() is None:
                            sub.kill()
                        if itr.poll() is None:
                            itr.kill()
                        break
                    if sub.poll() is not None and itr.poll() is not None:
                        break
                    time.sleep(0.01)
            finally:
                for stream in (sub.stdin, sub.stdout, itr.stdin, itr.stdout):
                    if stream is None:
                        continue
                    try:
                        stream.close()
                    except Exception:
                        pass

            for proc in [sub, itr]:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass

            sub_err_fh.flush()
            itr_err_fh.flush()
            wall_ms = int((time.monotonic() - start) * 1000)
            _sub_mem_kb, user_ms = read_time_metrics(sub_time_path)
            if user_ms <= 0:
                user_ms = 0
            if timed_out:
                tf.write("timeout\n")
                if timeout_ms > 0:
                    if user_ms <= 0:
                        user_ms = timeout_ms
                    else:
                        user_ms = cap_tle_time_ms(max(user_ms, timeout_ms), timeout_ms)
                return "TL", user_ms, wall_ms, 0

            if sub.returncode != 0:
                err = sub_err_path.read_text(encoding="utf-8", errors="replace") if sub_err_path.exists() else ""
                tf.write(f"submission stderr:\n{err}\n")
                if timeout_ms > 0 and user_ms >= timeout_ms:
                    return "TL", cap_tle_time_ms(user_ms, timeout_ms), wall_ms, 0
                return "RE", user_ms, wall_ms, 0
            itr_verdict = validator_style_verdict(itr.returncode or 0)
            if itr_verdict != "OK":
                err = itr_err_path.read_text(encoding="utf-8", errors="replace") if itr_err_path.exists() else ""
                tf.write(f"interactor stderr:\n{err}\n")
                if timeout_ms > 0 and user_ms >= timeout_ms:
                    return "TL", cap_tle_time_ms(user_ms, timeout_ms), wall_ms, 0
                return itr_verdict, user_ms, wall_ms, 0
            if timeout_ms > 0 and user_ms >= timeout_ms:
                return "TL", cap_tle_time_ms(user_ms, timeout_ms), wall_ms, 0
            if itr_output.exists():
                shutil.copy2(itr_output, interactor_output)
            return "OK", user_ms, wall_ms, 0


def run_multi_pass_interactive_test(
    *,
    run_interactive_case_fn: Callable[..., tuple[str, int, int, int]],
    run_checker: Callable[..., tuple[object, str, bool]],
    validator_style_verdict: Callable[[int], str],
    files_equal: Callable[[Path, Path], bool],
    feedback_message_for_pass: Callable[[Path, Path], str],
    feedback_key_files: Callable[[Path, Path], list[str]],
    cap_tle_time_ms: Callable[[int, int], int],
    sub_bin: Path,
    interactor: Path,
    checker: Path,
    checker_args: list[str],
    max_passes: int,
    run_timeout_sec: int,
    run_timeout_ms: int,
    run_memory_mb: int,
    run_process_limit: int,
    run_output_kb: int,
    test: Path,
    ans: Path,
    test_feedback_dir: Path,
    run_root: Path,
) -> dict:
    test_feedback_dir.mkdir(parents=True, exist_ok=True)
    test_result = {
        "test": test.name,
        "passes": [],
        "verdict": "OK",
        "sandbox_status": "ok",
        "time_ms": 0,
        "time_user_ms": 0,
        "time_wall_ms": 0,
        "memory_kb": 0,
        "feedback_files": [],
    }
    current_input = test
    pass_idx = 1
    total_user_ms = 0
    total_wall_ms = 0
    peak_mem = 0
    final_pass_row: dict[str, object] | None = None
    while True:
        pass_feedback_dir = test_feedback_dir
        pass_feedback_dir.mkdir(parents=True, exist_ok=True)
        interactor_output = run_root / f"{test.stem}.out"
        transcript = run_root / f"{test.stem}.transcript.txt"
        verdict, exec_user_ms, exec_wall_ms, exec_mem = run_interactive_case_fn(
            interactor,
            sub_bin,
            current_input,
            ans,
            interactor_output,
            transcript,
            pass_feedback_dir,
            timeout_sec=run_timeout_sec,
            timeout_ms=run_timeout_ms,
            memory_mb=run_memory_mb,
            process_limit=run_process_limit,
            output_kb=run_output_kb,
        )
        total_user_ms += exec_user_ms
        total_wall_ms += exec_wall_ms
        peak_mem = max(peak_mem, exec_mem)
        interactive_feedback = feedback_message_for_pass(pass_feedback_dir / "interactor", run_root)
        if not interactive_feedback:
            interactive_feedback = feedback_message_for_pass(pass_feedback_dir, run_root)
        if verdict != "OK":
            p = {
                "verdict": verdict,
                "time_ms": exec_user_ms,
                "time_user_ms": exec_user_ms,
                "time_wall_ms": exec_wall_ms,
                "memory_kb": exec_mem,
            }
            if interactive_feedback:
                p["feedback"] = interactive_feedback
            final_pass_row = p
            test_result["verdict"] = verdict
            if verdict == "TL":
                test_result["sandbox_status"] = "tle"
            elif verdict == "RE":
                test_result["sandbox_status"] = "re"
            elif verdict == "FL":
                test_result["sandbox_status"] = "fail"
            break

        checker_verdict = "OK"
        if checker.exists():
            check_result, checker_log, checker_timed_out = run_checker(
                checker,
                checker_args=checker_args,
                test=test,
                team_output=interactor_output,
                answer=ans,
                pass_feedback_dir=pass_feedback_dir,
                run_timeout_sec=run_timeout_sec,
                run_memory_mb=run_memory_mb,
                run_process_limit=run_process_limit,
                run_output_kb=run_output_kb,
            )
            (pass_feedback_dir / "checker.log").write_text(checker_log, encoding="utf-8")
            if checker_timed_out:
                checker_verdict = "FL"
            else:
                checker_verdict = validator_style_verdict(int(check_result.returncode or 0))
        else:
            checker_verdict = "OK" if files_equal(ans, interactor_output) else "WA"

        p = {
            "verdict": checker_verdict,
            "time_ms": exec_user_ms,
            "time_user_ms": exec_user_ms,
            "time_wall_ms": exec_wall_ms,
            "memory_kb": exec_mem,
        }
        pass_feedback = feedback_message_for_pass(pass_feedback_dir, run_root)
        if not pass_feedback:
            pass_feedback = interactive_feedback
        if pass_feedback:
            p["feedback"] = pass_feedback
        final_pass_row = p
        if checker_verdict == "OK":
            test_result["verdict"] = "OK"
            break
        if pass_idx >= max_passes:
            test_result["verdict"] = checker_verdict
            break
        has_next_input = False
        try:
            has_next_input = interactor_output.exists() and interactor_output.is_file() and interactor_output.stat().st_size > 0
        except OSError:
            has_next_input = False
        if not has_next_input:
            test_result["verdict"] = checker_verdict
            break
        current_input = interactor_output
        pass_idx += 1

    if final_pass_row is not None:
        test_result["passes"] = [final_pass_row]
    if str(test_result.get("verdict") or "").strip().upper().startswith("TL"):
        total_user_ms = cap_tle_time_ms(total_user_ms, run_timeout_ms)
    test_result["time_ms"] = total_user_ms
    test_result["time_user_ms"] = total_user_ms
    test_result["time_wall_ms"] = total_wall_ms
    test_result["memory_kb"] = peak_mem
    test_result["feedback_files"] = feedback_key_files(test_feedback_dir, run_root)
    return test_result
