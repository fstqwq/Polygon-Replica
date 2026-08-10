from __future__ import annotations

import atexit
import json
import os
import shutil
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

_TESTSUITE_BASE = Path("/tmp/polygon-replica")
_TESTSUITE_ROOT = Path(
    os.environ.get(
        "POLYGON_REPLICA_TESTSUITE_ROOT",
        str(_TESTSUITE_BASE / f"testsuite-{uuid.uuid4().hex[:8]}"),
    )
).resolve()
os.environ["POLYGON_REPLICA_TESTSUITE_ROOT"] = str(_TESTSUITE_ROOT)
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
    os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"] = str(
        root / "var" / "lib" / "polygon-replica" / "artifacts"
    )
    os.environ["POLYGON_REPLICA_CACHE_ROOT"] = str(root / "var" / "cache" / "polygon-replica")
    os.environ["POLYGON_REPLICA_CONTEST_SOURCE_ROOT"] = str(
        root / "var" / "lib" / "polygon-replica" / "contest-sources"
    )
    os.environ["POLYGON_REPLICA_BACKUP_ROOT"] = str(
        root / "var" / "backups" / "polygon-replica"
    )


ensure_local_env()

from app.impl.runtime.config import config  # noqa: E402


_COMPLETION_REF_ABORT_TRIGGER = "test_abort_verification_completion_ref_insert"
_ACTIVATION_TASK_ABORT_TRIGGER = "test_abort_verification_activation_task_insert"
_STARTUP_RECOVERY_ABORT_TRIGGER = "test_abort_verification_startup_recovery"


def install_completion_ref_abort_fault() -> None:
    """Force completion commits to fail while inserting artifact refs."""

    config.db.execute(
        f"""
        CREATE TRIGGER {_COMPLETION_REF_ABORT_TRIGGER}
        BEFORE INSERT ON verification_artifact_refs
        BEGIN
            SELECT RAISE(ABORT, 'forced artifact ref failure');
        END
        """
    )


def clear_completion_ref_abort_fault() -> None:
    """Remove the completion fault installed by the matching test helper."""

    config.db.execute(
        f"DROP TRIGGER IF EXISTS {_COMPLETION_REF_ABORT_TRIGGER}"
    )


def install_activation_task_abort_fault() -> None:
    """Force activation to fail while inserting its immutable task graph."""

    config.db.execute(
        f"""
        CREATE TRIGGER {_ACTIVATION_TASK_ABORT_TRIGGER}
        BEFORE INSERT ON verification_tasks
        BEGIN
            SELECT RAISE(ABORT, 'forced activation task failure');
        END
        """
    )


def clear_activation_task_abort_fault() -> None:
    """Remove the activation fault installed by the matching test helper."""

    config.db.execute(
        f"DROP TRIGGER IF EXISTS {_ACTIVATION_TASK_ABORT_TRIGGER}"
    )


def install_startup_recovery_abort_fault() -> None:
    """Force startup recovery to roll back its aggregate transition."""

    config.db.execute(
        f"""
        CREATE TRIGGER {_STARTUP_RECOVERY_ABORT_TRIGGER}
        BEFORE UPDATE OF status ON verifications
        WHEN OLD.status IN ('queued','running')
        BEGIN
            SELECT RAISE(ABORT, 'forced startup recovery failure');
        END
        """
    )


def clear_startup_recovery_abort_fault() -> None:
    """Remove the startup recovery fault installed by the matching helper."""

    config.db.execute(
        f"DROP TRIGGER IF EXISTS {_STARTUP_RECOVERY_ABORT_TRIGGER}"
    )
import app.impl.auth.password_envelope as password_envelope_module  # noqa: E402
from app.impl.auth.password_envelope import PasswordEnvelopeStore  # noqa: E402
from app.service.platform.testlib_source import maintained_testlib_header  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402


def _expected_test_db_path() -> Path:
    return Path(os.environ["POLYGON_REPLICA_DB"]).resolve()


def _assert_test_runtime_paths() -> None:
    db_path = Path(config.db.path).resolve()
    expected_db_path = _expected_test_db_path()
    if db_path != expected_db_path:
        raise RuntimeError(
            f"test DB path mismatch: config={db_path}, expected={expected_db_path}"
        )
    if suite_root().resolve() not in db_path.parents:
        raise RuntimeError(f"test DB must stay inside testsuite root: {db_path}")


_assert_test_runtime_paths()

# Full-runtime tests exercise the envelope protocol, not RSA key generation cost.
# The production defaults remain covered separately by an explicit contract test.
_TEST_PASSWORD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
password_envelope_module.password_envelope_store = PasswordEnvelopeStore(
    key_factory=lambda: _TEST_PASSWORD_KEY
)
_test_runtime_values = config.constants.to_dict()
_test_runtime_values["PASSWORD_HASH_ITERS"] = 10_000
config.constants.replace(_test_runtime_values)


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


_DB_TEMPLATE_PATH = suite_root() / "fixture-template" / "metadata.db"


def _checkpoint_database() -> None:
    _assert_test_runtime_paths()
    with db.conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _database_sidecars(path: Path) -> tuple[Path, Path]:
    return (Path(f"{path}-wal"), Path(f"{path}-shm"))


def _initialize_database_template() -> None:
    db.init()
    _checkpoint_database()
    _DB_TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db.path, _DB_TEMPLATE_PATH)


def _restore_database_template() -> None:
    _assert_test_runtime_paths()
    for sidecar in _database_sidecars(db.path):
        sidecar.unlink(missing_ok=True)
    replacement = db.path.with_name(f".{db.path.name}.{uuid.uuid4().hex}.tmp")
    replacement.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(_DB_TEMPLATE_PATH, replacement)
        os.replace(replacement, db.path)
    finally:
        replacement.unlink(missing_ok=True)


def _clear_runtime_files() -> None:
    roots = {
        Path(config.settings.bare_root),
        Path(config.settings.workspace_root),
        Path(config.settings.artifacts_root),
        Path(config.settings.cache_root),
        Path(config.settings.contest_source_root),
        Path(config.settings.backup_root),
    }
    for root in roots:
        _rmtree_retry(root)


_initialize_database_template()


class RuntimeDBTestBase(unittest.TestCase):
    """Database reset shared by fixtures that use the global runtime graph."""

    def setUp(self) -> None:
        _restore_database_template()
        self.test_id = uuid.uuid4().hex[:8]
        self.user = self.random_id("alice")
        self.problem = f"{self.user}/{self.random_id('sample')}"
        self.default_user = "alice"
        self.default_problem = "alice/sample"

    def random_id(self, prefix: str) -> str:
        safe_prefix = str(prefix or "").strip("-")[:7] or "user"
        return f"{safe_prefix}-{uuid.uuid4().hex[:8]}"

    def _artifact_root(self, artifact_id: str) -> Path:
        problem = str(getattr(self, "problem", "alice/sample"))
        return Path(os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"]) / problem / artifact_id


class WorkspaceTestBase(RuntimeDBTestBase):
    """DB fixture that creates real Git workspaces only when requested."""

    allow_worker_submit = False

    def _seed_workspace(self, problem: str, user: str, *, profile: str = "full") -> Path:
        if profile not in {"repository", "statement", "verification", "full"}:
            raise ValueError(f"unknown workspace seed profile: {profile}")
        workspace_service.ensure_problem(problem)
        ws = Path(workspace_service.ensure_workspace(problem, user))
        workspace_service.grant_repo_access(problem, user, "owner")
        if profile == "repository":
            return ws

        rel_paths: list[str] = []
        if profile in {"statement", "full"}:
            rel_paths.extend(["statement", "statement-sections/english"])
        if profile in {"verification", "full"}:
            rel_paths.extend(
                [
                    "config",
                    "validators",
                    "checkers",
                    "interactors",
                    "generators",
                    "solutions",
                    "tests/manual",
                    "tests/generator",
                    "third_party/testlib",
                ]
            )
        for rel in rel_paths:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        if profile in {"statement", "full"}:
            self._seed_statement_files(ws)
        if profile in {"verification", "full"}:
            self._seed_verification_files(ws)
        return ws

    @staticmethod
    def _seed_statement_files(ws: Path) -> None:
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
                "\\begin{problem}{${problem.name}}{}"
                "{${problem.inputFile}}{${problem.outputFile}}{${problem.timeLimit}}\n"
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

    @staticmethod
    def _seed_verification_files(ws: Path) -> None:
        problem_cfg = ws / "config/problem.json"
        problem_cfg_payload: dict[str, object] = {
            "input_file": "stdin",
            "memory_limit_mb": 1024,
            "mode": "pass-fail",
            "output_file": "stdout",
            "pass_limit": 1,
            "time_limit_ms": 2000,
        }
        problem_cfg.write_text(
            json.dumps(problem_cfg_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        testlib = ws / "third_party/testlib/testlib.h"
        if not testlib.exists():
            source = maintained_testlib_header(repo_root=Path(__file__).resolve().parents[1])
            testlib.write_bytes(source.read_bytes())

    def _workspace_path(self) -> Path:
        problem = str(getattr(self, "problem", "alice/sample"))
        user = str(getattr(self, "user", "alice"))
        try:
            ctx = workspace_service.workspace_context(problem, user, include_recent=False)
        except Exception:
            return self._seed_workspace(problem, user)
        return Path(str(ctx["workspace"]["path"]))

    def setUp(self) -> None:
        super().setUp()
        workspace_service.clear_identity_caches()
        if not self.allow_worker_submit:
            submit_guard = patch.object(
                config.worker_queue_service,
                "submit",
                side_effect=AssertionError("workspace tests may not submit worker jobs"),
            )
            submit_guard.start()
            self.addCleanup(submit_guard.stop)


class WorkerTestBase(WorkspaceTestBase):
    """Workspace fixture that owns and drains asynchronous runtime workers."""

    allow_worker_submit = True

    def setUp(self) -> None:
        _wait_for_verification_workers(timeout_sec=10.0)
        _wait_for_export_workers(timeout_sec=10.0)
        config.judgehost_task_service.reset_runtime_state()
        self._clear_test_files()
        super().setUp()
        self.addCleanup(self._cleanup_workers)

    def _clear_test_files(self) -> None:
        pass

    @staticmethod
    def _cleanup_workers() -> None:
        _wait_for_verification_workers(timeout_sec=10.0)
        _wait_for_export_workers(timeout_sec=10.0)
        config.judgehost_task_service.reset_runtime_state()


class E2ETestBase(WorkerTestBase):
    """Full application fixture retained only for end-to-end tests."""

    seed_primary_workspace = True
    seed_default_workspace = False

    def _clear_test_files(self) -> None:
        _clear_runtime_files()

    def setUp(self) -> None:
        super().setUp()
        old_long_poll = config.judgehost_task_service.state.fetch_long_poll_sec
        config.judgehost_task_service.state.fetch_long_poll_sec = 0.0
        self.addCleanup(
            setattr,
            config.judgehost_task_service.state,
            "fetch_long_poll_sec",
            old_long_poll,
        )
        if self.seed_primary_workspace:
            self._seed_workspace(self.problem, self.user)
        if self.seed_default_workspace:
            self._seed_workspace(self.default_problem, self.default_user)
