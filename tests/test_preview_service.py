from __future__ import annotations

import json
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, cast

from app.db import now_iso
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec
from app.service.sandbox.base import ExecResult, ExecSpec, SandboxBackend
from app.service.statement.preview import PreviewService
from app.service.statement.render import seed_statement_sources
from app.service.statement.tex_compile import TexCompileService
from app.service.repository.workspace import WorkspaceService
from app.setting import Settings
from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import isolated_db_execute, isolated_db_fetch_one

if TYPE_CHECKING:
    from app.service.verification.service import VerificationService


_HEAD = "a" * 40
_TESTS_SPEC_MAX_BYTES = 256 * 1024
_STATEMENT_SAMPLE_MAX_BYTES = 32 * 1024


class _LocalWorkspaceService:
    """Small workspace boundary with no Git process or global runtime."""

    def __init__(
        self,
        settings: Settings,
        *,
        workspace: Path,
        problem_id: int,
        workspace_id: int,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.problem_id = problem_id
        self.workspace_id = workspace_id
        self.dirty = True

    def workspace_context(
        self,
        problem: str,
        username: str,
        include_recent: bool = True,
    ) -> dict[str, object]:
        _ = include_recent
        return {
            "problem": {"id": self.problem_id, "slug": problem},
            "user": {"username": username},
            "workspace": {
                "id": self.workspace_id,
                "path": str(self.workspace),
                "branch": "main",
            },
            "latest_artifact_verification": None,
        }

    @contextmanager
    def workspace_lock(self, workspace: Path) -> Iterator[None]:
        if workspace != self.workspace:
            raise AssertionError("unexpected workspace")
        yield

    def read_workspace_status(self, workspace: Path) -> dict[str, object]:
        if workspace != self.workspace:
            raise AssertionError("unexpected workspace")
        return {"branch": "main", "head_commit": _HEAD, "dirty": self.dirty}

    def create_snapshot(
        self,
        workspace: Path,
        commit: str | None,
        workspace_head: str | None = None,
        workspace_dirty: bool | None = None,
    ) -> Path:
        _ = (commit, workspace_head, workspace_dirty)
        snapshot = (
            self.settings.cache_root
            / "runtime"
            / "snapshots"
            / f"snapshot-{uuid.uuid4().hex[:12]}"
            / "src"
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(workspace, snapshot)
        return snapshot


class _FakeTexSandbox(SandboxBackend):
    name = "fake-tex"

    def __init__(self) -> None:
        self.calls: list[ExecSpec] = []
        self.handler: Callable[[ExecSpec], ExecResult] = self._success

    def _success(self, spec: ExecSpec) -> ExecResult:
        cwd = Path(spec.cwd or ".")
        stem = Path(str(spec.command[-1])).stem
        (cwd / f"{stem}.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (cwd / f"{stem}.log").write_text("ok\n", encoding="utf-8")
        return ExecResult(
            backend=self.name,
            status="ok",
            returncode=0,
            elapsed_ms=1,
        )

    def run(self, spec: ExecSpec) -> ExecResult:
        self.calls.append(spec)
        return self.handler(spec)


class _VerificationArtifacts:
    def __init__(
        self,
        verification_id: str,
        refs: dict[tuple[str, str], str],
        descriptors: dict[str, PayloadFile],
    ) -> None:
        self.verification_id = verification_id
        self.refs = refs
        self.descriptors = descriptors
        self.calls: list[tuple[str, str, bool]] = []

    def run_verification(
        self,
        problem: str,
        username: str,
        commit: str | None = None,
        ref: str | None = None,
        *,
        sample_only: bool = False,
    ) -> str:
        _ = (commit, ref)
        self.calls.append((problem, username, sample_only))
        return self.verification_id

    def verification_artifact_ref(
        self,
        verification_id: str,
        test_name: str,
        ref_key: str,
    ) -> str:
        if verification_id != self.verification_id:
            raise AssertionError("unexpected verification")
        return self.refs.get((test_name, ref_key), "")

    def artifact_descriptor(self, token: str) -> PayloadFile | None:
        return self.descriptors.get(token)

    def verification_detail(self, verification_id: str) -> dict[str, object]:
        if verification_id != self.verification_id:
            raise AssertionError("unexpected verification")
        return {}


class TestPreviewService(DBTestBase):
    def setUp(self) -> None:
        super().setUp()
        created_at = now_iso()
        isolated_db_execute(
            self.db,
            "INSERT INTO users(username,email,email_normalized,created_at) VALUES(?,?,?,?)",
            [self.user, f"{self.user}@example.test", f"{self.user}@example.test", created_at],
        )
        isolated_db_execute(
            self.db,
            "INSERT INTO problems(slug,repo_name,created_at) VALUES(?,?,?)",
            [self.problem, f"{uuid.uuid4().hex}.git", created_at],
        )
        user_row = isolated_db_fetch_one(
            self.db, "SELECT id FROM users WHERE username=?", [self.user]
        )
        problem_row = isolated_db_fetch_one(
            self.db, "SELECT id FROM problems WHERE slug=?", [self.problem]
        )
        if user_row is None or problem_row is None:
            raise AssertionError("fixture identity was not persisted")
        self.problem_id = int(problem_row["id"])
        self.workspace = self.settings.workspace_root / self.user / "sample"
        self.workspace.mkdir(parents=True, exist_ok=True)
        seed_statement_sources(self.workspace)
        (self.workspace / "config").mkdir(parents=True, exist_ok=True)
        (self.workspace / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (self.workspace / "tests" / "generator").mkdir(parents=True, exist_ok=True)
        isolated_db_execute(
            self.db,
            """
            INSERT INTO workspaces(problem_id,user_id,path,branch,head_commit,dirty,updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [self.problem_id, int(user_row["id"]), str(self.workspace), "main", _HEAD, 1, created_at],
        )
        workspace_row = isolated_db_fetch_one(
            self.db,
            "SELECT id FROM workspaces WHERE problem_id=? AND user_id=?",
            [self.problem_id, int(user_row["id"])],
        )
        if workspace_row is None:
            raise AssertionError("fixture workspace was not persisted")
        self.workspace_id = int(workspace_row["id"])
        self.local_workspace = _LocalWorkspaceService(
            self.settings,
            workspace=self.workspace,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )
        self.sandbox = _FakeTexSandbox()
        compiler = TexCompileService(
            sandbox_backend=self.sandbox,
            config_values=self.config_values,
        )
        self.service = PreviewService(
            self.db,
            cast(WorkspaceService, self.local_workspace),
            compiler,
        )

    def _insert_preview(
        self,
        preview_id: str,
        *,
        status: str,
        summary: dict[str, object],
        created_at: str,
        source_commit: str = _HEAD,
    ) -> Path:
        isolated_db_execute(
            self.db,
            """
            INSERT INTO previews(
                id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                self.problem_id,
                self.workspace_id,
                source_commit,
                "main",
                status,
                json.dumps(summary),
                created_at,
            ],
        )
        return self.fs_manager.prepare_preview_layout(preview_id).root

    def _preview_row(self, preview_id: str):
        row = isolated_db_fetch_one(
            self.db,
            "SELECT status,verification_id,summary_json FROM previews WHERE id=?",
            [preview_id],
        )
        if row is None:
            raise AssertionError(f"preview was not persisted: {preview_id}")
        return row

    def test_cached_preview_requires_matching_language_signature_and_artifacts(self) -> None:
        english_id = f"p-{uuid.uuid4().hex[:12]}"
        chinese_id = f"p-{uuid.uuid4().hex[:12]}"
        missing_id = f"p-{uuid.uuid4().hex[:12]}"
        for preview_id, language, created_at in (
            (english_id, "english", "2026-04-11T00:00:00Z"),
            (chinese_id, "chinese", "2026-04-11T00:00:01Z"),
            (missing_id, "english", "2026-04-11T00:00:02Z"),
        ):
            root = self._insert_preview(
                preview_id,
                status="ok",
                summary={
                    "pdf": "statement_preview/statement.pdf",
                    "statement_signature": "sig-123",
                    "language": language,
                },
                created_at=created_at,
            )
            if preview_id != missing_id:
                (root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
                (root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")

        resolved_english = self.service.find_cached_preview_id(
            self.problem,
            self.problem_id,
            self.workspace_id,
            language="english",
            source_commit=_HEAD,
            statement_signature="sig-123",
        )
        resolved_chinese = self.service.find_cached_preview_id(
            self.problem,
            self.problem_id,
            self.workspace_id,
            language="chinese",
            source_commit=_HEAD,
            statement_signature="sig-123",
        )

        self.assertEqual(resolved_english, english_id)
        self.assertEqual(resolved_chinese, chinese_id)
        self.assertIsNone(
            self.service.find_cached_preview_id(
                self.problem,
                self.problem_id,
                self.workspace_id,
                language="english",
                source_commit=_HEAD,
                statement_signature="different",
            )
        )

    def test_compile_preview_persists_pdf_log_and_stable_summary(self) -> None:
        preview_id = self.service.compile_preview(self.problem, self.user, language="english")

        row = self._preview_row(preview_id)
        summary = json.loads(str(row["summary_json"]))
        root = self.fs_manager.resolve_preview_root(preview_id)
        self.assertEqual(str(row["status"]), "ok")
        self.assertIsNone(row["verification_id"])
        self.assertEqual(summary["pdf"], "statement_preview/statement.pdf")
        self.assertEqual(summary["language"], "english")
        self.assertTrue(summary["statement_signature"])
        self.assertTrue(summary["preview_ref"])
        self.assertTrue((root / "statement_preview" / "statement.pdf").is_file())
        self.assertTrue((root / "logs" / "latex.log").is_file())
        self.assertEqual(len(self.sandbox.calls), 2)
        self.assertFalse(
            any(
                str(token).startswith("-output-directory=")
                for token in self.sandbox.calls[0].command
            )
        )

    def test_compile_preview_failure_persists_diagnostic_without_pdf(self) -> None:
        marker = "statement/main.tex:7 Undefined control sequence"
        prebuilt = self.workspace / "statement" / "rendered" / "english" / "problem.pdf"
        prebuilt.parent.mkdir(parents=True, exist_ok=True)
        prebuilt.write_bytes(b"%PDF-1.4\n% prebuilt\n%%EOF\n")

        def _fail(spec: ExecSpec) -> ExecResult:
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
            (cwd / f"{stem}.log").write_text(marker + "\n", encoding="utf-8")
            return ExecResult(
                backend="fake-tex",
                status="error",
                returncode=1,
                elapsed_ms=1,
            )

        self.sandbox.handler = _fail
        preview_id = self.service.compile_preview(self.problem, self.user, language="english")

        row = self._preview_row(preview_id)
        summary = json.loads(str(row["summary_json"]))
        root = self.fs_manager.resolve_preview_root(preview_id)
        log_text = (root / "logs" / "latex.log").read_text(encoding="utf-8")
        self.assertEqual(str(row["status"]), "failed")
        self.assertEqual(summary["returncode"], 1)
        self.assertEqual(summary["failed_stage"], "latex_compile")
        self.assertTrue(summary["statement_signature"])
        self.assertTrue(summary["preview_ref"])
        self.assertIn(marker, log_text)
        self.assertFalse((root / "statement_preview" / "statement.pdf").exists())

    def test_compile_preview_writes_fallback_log_for_empty_tex_failure(self) -> None:
        def _fail_with_empty_log(spec: ExecSpec) -> ExecResult:
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
            (cwd / f"{stem}.log").write_text("", encoding="utf-8")
            return ExecResult(
                backend="fake-tex",
                status="error",
                returncode=1,
                elapsed_ms=1,
            )

        self.sandbox.handler = _fail_with_empty_log
        preview_id = self.service.compile_preview(self.problem, self.user, language="english")

        root = self.fs_manager.resolve_preview_root(preview_id)
        log_text = (root / "logs" / "latex.log").read_text(encoding="utf-8").strip()
        self.assertEqual(str(self._preview_row(preview_id)["status"]), "failed")
        self.assertIn("latex compile failed", log_text.lower())

    def test_compile_preview_records_missing_tex_executable(self) -> None:
        def _missing(_spec: ExecSpec) -> ExecResult:
            raise FileNotFoundError("pdflatex missing")

        self.sandbox.handler = _missing
        preview_id = self.service.compile_preview(self.problem, self.user, language="english")

        row = self._preview_row(preview_id)
        summary = json.loads(str(row["summary_json"]))
        root = self.fs_manager.resolve_preview_root(preview_id)
        self.assertEqual(str(row["status"]), "failed")
        self.assertIn("pdflatex missing", summary["error"])
        self.assertFalse((root / "statement_preview" / "statement.pdf").exists())

    def test_compile_preview_does_not_reuse_signatureless_cache_row(self) -> None:
        legacy_id = f"p-{uuid.uuid4().hex[:12]}"
        root = self._insert_preview(
            legacy_id,
            status="ok",
            summary={"pdf": "statement_preview/statement.pdf", "language": "english"},
            created_at="2026-04-11T00:00:00Z",
            source_commit="",
        )
        (root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")

        preview_id = self.service.compile_preview(self.problem, self.user, language="english")

        self.assertNotEqual(preview_id, legacy_id)
        self.assertEqual(str(self._preview_row(preview_id)["status"]), "ok")

    def test_sample_sync_materializes_generated_payload_and_preserves_custom_input(self) -> None:
        (self.workspace / "tests" / "manual" / "903.in").write_text(
            "base-manual-input\n",
            encoding="utf-8",
        )
        (self.workspace / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {"id": "901", "kind": "manual", "sample": True},
                    {"id": "902", "kind": "gen", "sample": True},
                    {
                        "id": "903",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "custom-sample-input\n",
                    },
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )
        verification_id = f"v-{uuid.uuid4().hex}"
        isolated_db_execute(
            self.db,
            """
            INSERT INTO verifications(
                id,problem_id,workspace_id,signature,kind,status,created_at,finished_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                self.problem_id,
                self.workspace_id,
                "",
                "all",
                "ok",
                now_iso(),
                now_iso(),
            ],
        )
        raw_payloads = {
            ("001.in", "input_ref"): b"build-manual-input\n",
            ("001.in", "answer_ref"): b"build-manual-answer\n",
            ("002.in", "input_ref"): b"build-gen-input\n",
            ("002.in", "answer_ref"): b"build-gen-answer\n",
            ("003.in", "input_ref"): b"custom-sample-input\n",
            ("003.in", "answer_ref"): b"custom-sample-answer\n",
        }
        refs: dict[tuple[str, str], str] = {}
        descriptors: dict[str, PayloadFile] = {}
        for identity, content in raw_payloads.items():
            descriptor = self.runtime_blob_store.put_bytes(content)
            refs[identity] = descriptor.blob_ref
            descriptors[descriptor.blob_ref] = descriptor
        verification = _VerificationArtifacts(verification_id, refs, descriptors)
        self.service.verification_service = cast("VerificationService", verification)

        summary = self.service.sync_sample_payloads_for_snapshot(
            self.problem,
            self.user,
            self.workspace,
        )

        spec = load_tests_spec(
            self.workspace / "tests" / "spec.json",
            document_max_bytes=_TESTS_SPEC_MAX_BYTES,
            sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
        )
        self.assertEqual(verification.calls, [(self.problem, self.user, True)])
        self.assertEqual(summary["copied"], 3)
        self.assertEqual(
            (self.workspace / "tests" / "manual" / "901.in").read_text(encoding="utf-8"),
            "build-manual-input\n",
        )
        self.assertEqual(
            (self.workspace / "tests" / "generator" / "902.in").read_text(encoding="utf-8"),
            "build-gen-input\n",
        )
        self.assertEqual(
            (self.workspace / "tests" / "manual" / "903.in").read_text(encoding="utf-8"),
            "base-manual-input\n",
        )
        self.assertEqual(spec[0]["sample_output"], "build-manual-answer\n")
        self.assertEqual(spec[1]["sample_output"], "build-gen-answer\n")
        self.assertEqual(spec[2]["sample_output"], "custom-sample-answer\n")

    def test_interactive_sample_sync_does_not_start_verification(self) -> None:
        (self.workspace / "config" / "problem.json").write_text(
            json.dumps({"mode": "interactive"}),
            encoding="utf-8",
        )
        (self.workspace / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "901",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "play\n",
                        "sample_output": "take\n",
                    }
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )

        summary = self.service.sync_sample_payloads_for_snapshot(
            self.problem,
            self.user,
            self.workspace,
        )

        self.assertEqual(summary, {"sample_count": 0, "copied": 0, "verification_id": "", "skipped": "interactive"})

    def test_prune_removes_terminal_history_but_preserves_running_and_kept_rows(self) -> None:
        keep_id = f"p-{uuid.uuid4().hex[:12]}"
        running_id = f"p-{uuid.uuid4().hex[:12]}"
        done_id = f"p-{uuid.uuid4().hex[:12]}"
        keep_root = self._insert_preview(
            keep_id,
            status="ok",
            summary={},
            created_at="2026-03-05T00:00:02Z",
        )
        running_root = self._insert_preview(
            running_id,
            status="running",
            summary={},
            created_at="2026-03-05T00:00:01Z",
        )
        done_root = self._insert_preview(
            done_id,
            status="failed",
            summary={},
            created_at="2026-03-05T00:00:00Z",
        )
        for root in (keep_root, running_root, done_root):
            (root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

        self.service.prune_workspace_preview_history(
            self.problem,
            self.problem_id,
            self.workspace_id,
            keep_id,
        )

        self.assertIsNotNone(
            isolated_db_fetch_one(
                self.db, "SELECT id FROM previews WHERE id=?", [keep_id]
            )
        )
        self.assertIsNotNone(
            isolated_db_fetch_one(
                self.db, "SELECT id FROM previews WHERE id=?", [running_id]
            )
        )
        self.assertIsNone(
            isolated_db_fetch_one(
                self.db, "SELECT id FROM previews WHERE id=?", [done_id]
            )
        )
        self.assertTrue(keep_root.exists())
        self.assertTrue(running_root.exists())
        self.assertFalse(done_root.exists())
