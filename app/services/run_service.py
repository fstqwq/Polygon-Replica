from __future__ import annotations

import json
import shlex
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService


class RunService:
    def __init__(self, db: DB, workspace_service: WorkspaceService):
        self.db = db
        self.workspace_service = workspace_service

    def run_submission(
        self,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str,
        mode: str = "pass-fail",
    ) -> str:
        run_id = f"r-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username)
        artifact_root = Path(self.workspace_service.settings.artifacts_root) / problem / build_id
        run_root = Path(self.workspace_service.settings.artifacts_root) / problem / build_id / "logs" / f"run-{run_id}"
        run_root.mkdir(parents=True, exist_ok=True)

        self.db.execute(
            "INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [
                run_id,
                ctx["problem"]["id"],
                ctx["workspace"]["id"],
                build_id,
                mode,
                "running",
                str(run_root),
                now_iso(),
            ],
        )

        tests_dir = artifact_root / "tests"
        ans_dir = artifact_root / "ans"
        checker = artifact_root / "bin" / "checker"
        feedback_dir = run_root / "feedback_dir"
        feedback_dir.mkdir(parents=True, exist_ok=True)

        workspace = Path(ctx["workspace"]["path"])
        sub_src = workspace / submission_path
        sub_bin = run_root / "submission"
        cproc = run_cmd(["g++", "-O2", "-std=c++20", str(sub_src), "-o", str(sub_bin)])
        if cproc.returncode != 0:
            (run_root / "compile.log").write_text(cproc.stdout + cproc.stderr, encoding="utf-8")
            self.db.execute(
                "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["failed", json.dumps({"error": "compile_error"}), now_iso(), run_id],
            )
            return run_id

        verdicts = []
        for test in sorted(tests_dir.glob("*.in")):
            out = run_root / f"{test.stem}.out"
            exec_cmd = f"{shlex.quote(str(sub_bin))} < {shlex.quote(str(test))} > {shlex.quote(str(out))}"
            exec_proc = run_cmd(["bash", "-lc", exec_cmd], timeout=30)
            if exec_proc.returncode != 0:
                verdicts.append({"test": test.name, "verdict": "RE", "time_ms": 0, "memory_kb": 0})
                continue

            if checker.exists():
                ans = ans_dir / f"{test.stem}.ans"
                check_proc = run_cmd([str(checker), str(test), str(out), str(ans)], timeout=30)
                verdict = "OK" if check_proc.returncode == 0 else "WA"
            else:
                ans = ans_dir / f"{test.stem}.ans"
                verdict = "OK" if ans.exists() and ans.read_text(encoding="utf-8") == out.read_text(encoding="utf-8") else "WA"
            verdicts.append({"test": test.name, "verdict": verdict, "time_ms": 0, "memory_kb": 0})

            if mode == "multi-pass":
                marker = feedback_dir / "nextpass.in"
                if marker.exists():
                    shutil.copy2(marker, test)
            if mode == "interactive":
                transcript = run_root / f"{test.stem}.transcript.txt"
                transcript.write_text(
                    "interactive transcript placeholder; pass-fail execution used in current implementation\\n",
                    encoding="utf-8",
                )

        summary = {"mode": mode, "tests": verdicts, "feedback_dir": str(feedback_dir)}
        (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.db.execute(
            "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
            ["ok", json.dumps(summary), now_iso(), run_id],
        )
        return run_id
