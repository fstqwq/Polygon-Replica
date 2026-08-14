"""Pure task/case result projections shared by query and finalization."""

from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.domjudge.case_result import build_case_terminal_report
from app.service.judgehost.domjudge.case_result import build_missing_case_result
from app.service.judgehost.domjudge.case_result import decode_case_test_row
from app.service.judgehost.domjudge.case_result import execution_result_with_terminal_context
from app.service.judgehost.ports.completion import CaseTerminalReport
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.summary import summary_compile_diagnostics
from app.service.judgehost.task.summary import summary_text
from app.service.judgehost.task.summary import task_summary_for_row


def project_task_case_result(
    tasks: JudgehostTaskRegistry,
    batch_runtime: JudgehostBatchRuntime,
    task_id: str,
    test_name: str,
) -> CaseTerminalReport | None:
    """Build one immutable terminal view without changing either owner."""

    if not task_id:
        raise RuntimeError("judgehost task id is required")
    if not test_name:
        raise RuntimeError("judgehost test name is required")
    row = tasks.get(task_id)
    if row is None:
        raise RuntimeError("judgehost task disappeared")
    result = batch_runtime.case_result_for_task(task_id, test_name)
    if result is not None:
        summary = task_summary_for_row(
            tasks,
            row,
            run_id=row["run_id"],
            verification_id=row["verification_id"],
        )
        task_kind = summary_text(row["payload"], "task_kind")
        recovered_error = result.feedback_text
        summary_error = summary_text(summary, "error")
        if (
            not summary_error
            and recovered_error
            and result.runresult in {"checker-fail", "compare-error", "internal-error"}
        ):
            summary_error = recovered_error
        if (
            not summary_error
            and recovered_error
            and task_kind == "main-correct"
            and result.verdict != "OK"
        ):
            summary_error = recovered_error
        terminal_summary: dict[str, object] = {
            "source": summary_text(summary, "source"),
            "compile_log": summary_text(summary, "compile_log"),
            "compile_diagnostics": summary_compile_diagnostics(summary),
            "error": summary_error,
            "tests": [decode_case_test_row(result, test_name=test_name)],
        }
        if task_kind == "main-correct":
            run_status = "ok" if result.verdict == "OK" else "failed"
        elif result.verdict in {"CE", "FL"} or result.runresult in {
            "compiler-error",
            "checker-fail",
            "compare-error",
            "internal-error",
        }:
            run_status = "failed"
        else:
            run_status = "ok"
        return build_case_terminal_report(
            task_id=task_id,
            verification_id=row["verification_id"],
            run_id=row["run_id"],
            status=run_status,
            task_status=row["status"],
            error_text=summary_error,
            summary=terminal_summary,
            missing_case_result=False,
            execution_result=execution_result_with_terminal_context(
                result,
                summary=terminal_summary,
                error_text=summary_error,
            ),
        )
    if row["status"] not in {"failed", "completed"}:
        return None
    summary = task_summary_for_row(
        tasks,
        row,
        run_id=row["run_id"],
        verification_id=row["verification_id"],
    )
    detail = summary_text(summary, "error") or row["error_text"]
    if not detail:
        detail = f"judgehost case result missing for {test_name}"
    missing_summary: dict[str, object] = {
        "source": summary_text(summary, "source"),
        "compile_log": summary_text(summary, "compile_log"),
        "compile_diagnostics": summary_compile_diagnostics(summary),
        "error": detail,
        "tests": [],
    }
    return build_case_terminal_report(
        task_id=task_id,
        verification_id=row["verification_id"],
        run_id=row["run_id"],
        status="failed",
        task_status=row["status"],
        error_text=detail,
        summary=missing_summary,
        missing_case_result=True,
        execution_result=build_missing_case_result(
            summary=missing_summary,
            error_text=detail,
        ),
    )
