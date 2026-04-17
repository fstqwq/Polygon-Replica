from __future__ import annotations

import atexit
import json
import os
import shutil
import time
import unittest
import uuid
from pathlib import Path

from app.service.platform.testlib_source import maintained_testlib_header


_TESTSUITE_BASE = Path("/tmp/polygon-replica")
_TESTSUITE_ROOT = _TESTSUITE_BASE / f"testsuite-{uuid.uuid4().hex[:8]}"
_DEFAULT_TESTSUITE_STALE_TTL_SEC = 3600.0


def _rmtree_retry(path: Path, attempts: int = 3, delay_sec: float = 0.1) -> None:
    target = Path(path)
    for _ in range(max(1, int(attempts))):
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            return
        time.sleep(max(0.0, float(delay_sec)))
    shutil.rmtree(target, ignore_errors=True)


def _testsuite_stale_ttl_sec() -> float:
    raw = str(os.environ.get("POLYGON_REPLICA_TESTSUITE_STALE_TTL_SEC", "")).strip()
    if not raw:
        return _DEFAULT_TESTSUITE_STALE_TTL_SEC
    try:
        value = float(raw)
    except Exception:
        return _DEFAULT_TESTSUITE_STALE_TTL_SEC
    return max(0.0, value)


def _cleanup_stale_testsuite_roots(exclude: Path | None = None) -> None:
    base = _TESTSUITE_BASE
    if not base.exists():
        return
    ttl_sec = _testsuite_stale_ttl_sec()
    now = time.time()
    exclude_resolved = exclude.resolve() if exclude is not None else None
    for path in base.glob("testsuite-*"):
        if not path.is_dir():
            continue
        if exclude_resolved is not None:
            try:
                if path.resolve() == exclude_resolved:
                    continue
            except Exception:
                pass
        try:
            age_sec = now - float(path.stat().st_mtime)
        except Exception:
            age_sec = 0.0
        if age_sec < ttl_sec:
            continue
        _rmtree_retry(path)


def suite_root() -> Path:
    return _TESTSUITE_ROOT


def _cleanup_testsuite_root() -> None:
    _rmtree_retry(suite_root())


atexit.register(_cleanup_testsuite_root)
_cleanup_stale_testsuite_roots(exclude=suite_root())


def ensure_local_env() -> None:
    root = suite_root()
    os.environ["POLYGON_REPLICA_DB"] = str(root / "var" / "lib" / "polygon-replica" / "metadata.db")
    os.environ["POLYGON_REPLICA_BARE_ROOT"] = str(root / "srv" / "git")
    os.environ["POLYGON_REPLICA_WORKSPACE_ROOT"] = str(root / "srv" / "workspaces")
    os.environ["POLYGON_REPLICA_RUN_ROOT"] = str(root / "srv" / "runs")
    os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"] = str(root / "var" / "lib" / "polygon-replica" / "artifacts")
    os.environ["POLYGON_REPLICA_CACHE_ROOT"] = str(root / "var" / "cache" / "polygon-replica")
    os.environ["POLYGON_REPLICA_AUTH_COOKIE_SECURE"] = "1"


ensure_local_env()

from app.impl.runtime.config import config  # noqa: E402


def _wait_for_worker_group(lock_attr: str, workers_attr: str, timeout_sec: float = 300.0) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        lock = getattr(config, lock_attr)
        with lock:
            workers = [w for w in getattr(config, workers_attr) if w.is_alive()]
            current = getattr(config, workers_attr)
            current.clear()
            current.update(workers)
        if not workers:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        config.worker_queue_service.wait_for_futures(workers, timeout_sec=min(0.2, remaining))


def _wait_for_verification_workers(timeout_sec: float = 300.0) -> None:
    _wait_for_worker_group("verification_lock", "verification_workers", timeout_sec=timeout_sec)


def _wait_for_export_workers(timeout_sec: float = 300.0) -> None:
    _wait_for_worker_group("export_lock", "export_workers", timeout_sec=timeout_sec)

db = config.db
export_service = config.export_service
preview_service = config.preview_service
workspace_service = config.workspace_service


class SmokeBase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            _wait_for_verification_workers(timeout_sec=10.0)
        except Exception:
            pass
        try:
            _wait_for_export_workers(timeout_sec=10.0)
        except Exception:
            pass
        config.judgehost_task_service.reset_runtime_state()
        with workspace_service._cache_lock:
            workspace_service._problem_cache.clear()
            workspace_service._user_cache.clear()
        _cleanup_stale_testsuite_roots(exclude=suite_root())
        _cleanup_testsuite_root()
        self.addCleanup(_cleanup_testsuite_root)
        db.init()
        self.test_id = uuid.uuid4().hex[:8]
        self.user = self.random_id("alice")
        self.problem = f"{self.user}/{self.random_id('sample')}"
        self.default_user = "alice"
        self.default_problem = "alice/sample"

        self._seed_workspace(self.problem, self.user)
        self._seed_workspace(self.default_problem, self.default_user)

    def _seed_workspace(self, problem: str, user: str) -> Path:
        workspace_service.ensure_problem(problem)
        ws = Path(workspace_service.ensure_workspace(problem, user))
        workspace_service.grant_repo_access(problem, user, "owner")
        for rel in [
            "statement",
            "statement-sections/english",
            "config",
            "validators",
            "checkers",
            "interactors",
            "generators",
            "solutions",
            "tests/manual",
            "tests/generator",
            "tests/answers",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)
        statement_template = ws / "statement/statements.ftl"
        if not statement_template.exists():
            statement_template.write_text(
                "\\documentclass{article}\n"
                "\\usepackage{olymp}\n"
                "\\begin{document}\n"
                "\\input{rendered/english/problem.tex}\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
        statement_problem = ws / "statement/problem.tex"
        if not statement_problem.exists():
            statement_problem.write_text(
                "\\begin{problem}{${problem.name}}{}{${problem.inputFile}}{${problem.outputFile}}{${problem.timeLimit}}\n"
                "${problem.legend}\n"
                "\\InputFile\n${problem.input}\n"
                "\\OutputFile\n${problem.output}\n"
                "\\end{problem}\n",
                encoding="utf-8",
            )
        statement_style = ws / "statement/olymp.sty"
        if not statement_style.exists():
            statement_style.write_text("% minimal olymp style for tests\n", encoding="utf-8")
        for rel, content in {
            "statement-sections/english/name.tex": "Sample Problem\n",
            "statement-sections/english/legend.tex": "Legend.\n",
            "statement-sections/english/input.tex": "Input.\n",
            "statement-sections/english/output.tex": "Output.\n",
            "statement-sections/english/notes.tex": "",
        }.items():
            path = ws / rel
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        problem_cfg = ws / "config/problem.json"
        problem_cfg_payload: dict[str, object] = {}
        if problem_cfg.exists():
            try:
                loaded = json.loads(problem_cfg.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    problem_cfg_payload = dict(loaded)
            except Exception:
                problem_cfg_payload = {}
        if "mode" not in problem_cfg_payload:
            problem_cfg_payload["mode"] = "pass-fail"
        if "pass_limit" not in problem_cfg_payload:
            problem_cfg_payload["pass_limit"] = 1
        if "time_limit_ms" not in problem_cfg_payload:
            problem_cfg_payload["time_limit_ms"] = 2000
        if "memory_limit_mb" not in problem_cfg_payload:
            problem_cfg_payload["memory_limit_mb"] = 1024
        if "input_file" not in problem_cfg_payload:
            problem_cfg_payload["input_file"] = "stdin"
        if "output_file" not in problem_cfg_payload:
            problem_cfg_payload["output_file"] = "stdout"
        problem_cfg.write_text(
            json.dumps(problem_cfg_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        testlib = ws / "third_party/testlib/testlib.h"
        if not testlib.exists():
            testlib.write_bytes(maintained_testlib_header(repo_root=Path(__file__).resolve().parents[1]).read_bytes())
        return ws

    def random_id(self, prefix: str) -> str:
        safe_prefix = str(prefix or "").strip("-")[:7] or "user"
        return f"{safe_prefix}-{uuid.uuid4().hex[:8]}"

    def _artifact_root(self, artifact_id: str) -> Path:
        problem = str(getattr(self, "problem", "alice/sample"))
        return Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / problem / artifact_id

    def _workspace_path(self) -> Path:
        problem = str(getattr(self, "problem", "alice/sample"))
        user = str(getattr(self, "user", "alice"))
        ctx = workspace_service.workspace_context(problem, user, include_recent=False)
        return Path(str(ctx["workspace"]["path"]))
