from app.db import now_iso
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.retention import compact_payload_for_retention
from app.service.judgehost.task.summary import load_run_summary
from app.service.judgehost.task.summary import summary_error_text
from app.service.judgehost.task.result_model import TaskFinalizationPayload, TaskSummary


class JudgehostTaskTerminalization:
    """Commit one task's canonical terminal result to the task registry."""

    def __init__(self, tasks: JudgehostTaskRegistry) -> None:
        self._tasks = tasks

    def finalize_task(
        self,
        *,
        task_id: str,
        payload: TaskFinalizationPayload,
    ) -> None:
        if not task_id:
            raise RuntimeError("task_id is required")
        raw_status = payload.get("run_status")
        if not isinstance(raw_status, str) or not raw_status:
            raise RuntimeError("judgehost run_status must be a non-empty string")
        succeeded = raw_status.lower() in {
            "ok", "accepted", "pass", "passed", "success", "completed"
        }
        run_status = "ok" if succeeded else "failed"
        task_status = "completed" if succeeded else "failed"
        raw_error = payload.get("error")
        if raw_error is None:
            error_text = ""
        elif isinstance(raw_error, str):
            error_text = raw_error.strip()
        else:
            raise RuntimeError("judgehost error must be a string")
        payload_summary = payload["summary"]

        row = self._tasks.claim_reporting(task_id, now_text=now_iso())
        if row is None:
            raise RuntimeError("judgehost task not found")
        if row["status"] in {"completed", "failed"}:
            return

        try:
            existing = load_run_summary(
                self._tasks,
                row["run_id"],
                row["verification_id"],
            ) or row["summary"].copy()
            summary: TaskSummary = {**existing, **payload_summary}
            if run_status != "ok":
                if error_text:
                    summary["error"] = error_text
                elif "error" not in summary:
                    summary["error"] = "judgehost reported failure"
            summary["status"] = run_status
            judgehost = summary.get("judgehost", {}).copy()
            judgehost.update(
                {
                    "task_id": task_id,
                    "hostname": "internal-finalizer",
                    "status": task_status,
                }
            )
            summary["judgehost"] = judgehost
            if not error_text and run_status != "ok":
                error_text = summary_error_text(summary) or "judgehost task failed"
            finished_at = now_iso()
        except Exception:
            self._tasks.restore_reporting(task_id, row, now_text=now_iso())
            raise

        completed = self._tasks.transition(
            task_id,
            expected={"reporting"},
            status=task_status,
            updates={
                "payload": compact_payload_for_retention(row["payload"]),
                "result": {
                    "run_status": run_status,
                    "error": error_text,
                    "summary": summary.copy(),
                },
                "summary": summary.copy(),
                "run_status": run_status,
                "error_text": error_text,
                "updated_at": finished_at,
                "completed_at": finished_at,
            },
        )
        if completed is None:
            raise RuntimeError("judgehost task reporting claim was lost")
