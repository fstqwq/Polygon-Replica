from __future__ import annotations

from pathlib import Path
from typing import Callable


def run_noninteractive_test(
    *,
    run_timeout_sentinel: int,
    run_pass: Callable[..., tuple[int, int, int, int]],
    run_checker: Callable[..., tuple[object, str, bool]],
    validator_style_verdict: Callable[[int], str],
    files_equal: Callable[[Path, Path], bool],
    feedback_message_for_pass: Callable[[Path, Path], str],
    feedback_key_files: Callable[[Path, Path], list[str]],
    cap_tle_time_ms: Callable[[int, int], int],
    mode: str,
    sub_bin: Path,
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
    guard_deny_paths = [ans.parent]
    guard_allow_paths = [run_root]
    final_pass_row: dict[str, object] | None = None
    while True:
        pass_feedback_dir = test_feedback_dir
        pass_feedback_dir.mkdir(parents=True, exist_ok=True)
        out = run_root / f"{test.stem}.out"
        time_file = pass_feedback_dir / "time.txt"
        exec_rc, exec_user_ms, exec_wall_ms, exec_mem = run_pass(
            sub_bin,
            current_input,
            out,
            run_root,
            run_timeout_sec,
            time_file,
            run_memory_mb,
            run_process_limit,
            run_output_kb,
            deny_paths=guard_deny_paths,
            allow_paths=guard_allow_paths,
        )
        if exec_rc != run_timeout_sentinel and run_timeout_ms > 0:
            if int(exec_user_ms) >= int(run_timeout_ms):
                exec_rc = run_timeout_sentinel
        total_user_ms += exec_user_ms
        total_wall_ms += exec_wall_ms
        peak_mem = max(peak_mem, exec_mem)
        if exec_rc == run_timeout_sentinel:
            capped_user_ms = cap_tle_time_ms(exec_user_ms, run_timeout_ms)
            if capped_user_ms != exec_user_ms:
                total_user_ms = max(0, total_user_ms - (exec_user_ms - capped_user_ms))
            p = {
                "verdict": "TL",
                "time_ms": capped_user_ms,
                "time_user_ms": capped_user_ms,
                "time_wall_ms": exec_wall_ms,
                "memory_kb": exec_mem,
            }
            pass_feedback = feedback_message_for_pass(pass_feedback_dir, run_root)
            if pass_feedback:
                p["feedback"] = pass_feedback
            final_pass_row = p
            test_result["verdict"] = "TL"
            test_result["sandbox_status"] = "tle"
            break
        if exec_rc != 0:
            p = {
                "verdict": "RE",
                "time_ms": exec_user_ms,
                "time_user_ms": exec_user_ms,
                "time_wall_ms": exec_wall_ms,
                "memory_kb": exec_mem,
            }
            pass_feedback = feedback_message_for_pass(pass_feedback_dir, run_root)
            if pass_feedback:
                p["feedback"] = pass_feedback
            final_pass_row = p
            test_result["verdict"] = "RE"
            test_result["sandbox_status"] = "re"
            break

        checker_verdict = "OK"
        if checker.exists():
            check_result, checker_log, checker_timed_out = run_checker(
                checker,
                checker_args=checker_args,
                test=test,
                team_output=out,
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
            checker_verdict = "OK" if files_equal(ans, out) else "WA"

        p = {
            "verdict": checker_verdict,
            "time_ms": exec_user_ms,
            "time_user_ms": exec_user_ms,
            "time_wall_ms": exec_wall_ms,
            "memory_kb": exec_mem,
        }
        pass_feedback = feedback_message_for_pass(pass_feedback_dir, run_root)
        if pass_feedback:
            p["feedback"] = pass_feedback
        final_pass_row = p

        if mode != "multi-pass":
            test_result["verdict"] = checker_verdict
            break

        next_pass = pass_feedback_dir / "nextpass.in"
        if checker_verdict != "OK" or not next_pass.exists() or pass_idx >= max_passes:
            test_result["verdict"] = checker_verdict
            break

        current_input = next_pass
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
