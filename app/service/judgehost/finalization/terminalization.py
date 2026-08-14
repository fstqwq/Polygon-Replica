from app.db import now_iso
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.retention import compact_payload_for_retention
from app.service.judgehost.task.retention import compact_task_row_payload
from app.service.judgehost.task.summary import load_run_summary
from app.service.judgehost.task.summary import summary_error_text
from app.service.judgehost.task.summary import summary_mapping


class JudgehostTaskTerminalization:
    """Commit one task's canonical terminal result to the task registry."""

    def __init__(self, tasks: JudgehostTaskRegistry) -> None:
        self._tasks = tasks

    @staticmethod
    def compact_payload_for_retention(payload: object) -> dict[str, object]:
        return compact_payload_for_retention(payload)

    def compact_task_payload(self, task_id: str) -> None:
        if not task_id:
            return
        row = self._tasks.get(task_id)
        if row is None:
            return
        compact_task_row_payload(row)
        self._tasks.update(task_id, {"payload": row["payload"]})

    def finalize_task(
        self,
        *,
        task_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        return self._commit_result(
            task_id=task_id,
            payload=payload,
        )

    def _commit_result(
        self,
        *,
        task_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
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
        raw_summary = payload.get("summary")
        payload_summary = None if raw_summary is None else summary_mapping(raw_summary)

        row = self._tasks.claim_reporting(task_id, now_text=now_iso())
        if row is None:
            raise RuntimeError("judgehost task not found")
        if row["status"] in {"completed", "failed"}:
            return {
                "task_id": task_id,
                "verification_id": row["verification_id"],
                "run_id": row["run_id"],
                "artifact_path": "",
                "status": row.get("run_status")
                or ("ok" if row["status"] == "completed" else "failed"),
                "summary": row["summary"].copy(),
            }

        try:
            existing = load_run_summary(
                self._tasks,
                row["run_id"],
                row["verification_id"],
            ) or row["summary"].copy()
            summary = existing if payload_summary is None else {**existing, **payload_summary}
            if run_status != "ok":
                if error_text:
                    summary["error"] = error_text
                elif "error" not in summary:
                    summary["error"] = "judgehost reported failure"
            summary["status"] = run_status
            judgehost = summary_mapping(summary.get("judgehost"))
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
                    "summary": dict(summary),
                },
                "summary": dict(summary),
                "run_status": run_status,
                "error_text": error_text,
                "updated_at": finished_at,
                "completed_at": finished_at,
            },
        )
        if completed is None:
            raise RuntimeError("judgehost task reporting claim was lost")
        return {
            "task_id": task_id,
            "verification_id": row["verification_id"],
            "run_id": row["run_id"],
            "artifact_path": "",
            "status": run_status,
            "summary": summary,
        }
