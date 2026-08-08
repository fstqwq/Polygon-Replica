"""Build and read Native materializations of published Git revisions.

This module owns the only bridge from a bare repository revision to a package.
Consumers receive a validated Native reader and never a workspace, Git handle, or
verification artifact reference.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypedDict

from app.db import DB, now_iso
from app.service.platform.fs.op import extract_git_archive
from app.service.platform.git_process import run_git
from app.service.platform.hashing import sha256_file
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.problem.test_spec import load_tests_spec
from app.service.problem_package.manifest import (
    NativeManifest,
    NativeTestEntry,
    describe_file,
    dumps_manifest,
    load_manifest,
    source_digest,
    validate_manifest_files,
)
from app.service.problem_package.store import MaterializationRow, ProblemPackageStore, PublishedProblem
from app.setting import Settings


VerificationBuilder = Callable[[Path, str, int, str], str]


@dataclass(frozen=True)
class PublishedRevision:
    problem: PublishedProblem
    source_commit: str
    revision_number: int
    bare_repo: Path


@dataclass(frozen=True)
class NativePackageReader:
    materialization: MaterializationRow
    root: Path
    manifest: NativeManifest

    def payload(self, test: NativeTestEntry, key: str) -> Path | None:
        descriptor = test.get(key)
        if descriptor is None:
            return None
        return self.root / Path(*PurePosixPath(str(descriptor["path"])).parts)


class ProblemReadiness(TypedDict):
    problem_id: int
    published_commit: str
    published_revision_number: int | None
    materialized_commit: str
    materialized_revision_number: int | None
    materialization_id: str
    archive_sha256: str
    current_is_materialized: bool
    statement_languages: list[str]
    missing_reason: str


class ProblemPackageService:
    """Materialize immutable published revisions and verify every read."""

    def __init__(
        self,
        db: DB,
        settings: Settings,
        artifact_file_resolver: Callable[[str], PayloadFile | None],
    ) -> None:
        self.db = db
        self.settings = settings
        self.store = ProblemPackageStore(db)
        self._artifact_file_resolver = artifact_file_resolver
        self._locks_guard = threading.Lock()
        self._build_locks: dict[tuple[int, str], threading.Lock] = {}

    def _build_lock(self, problem_id: int, source_commit: str) -> threading.Lock:
        key = (int(problem_id), source_commit)
        with self._locks_guard:
            return self._build_locks.setdefault(key, threading.Lock())

    def _bare_repo(self, problem: PublishedProblem) -> Path:
        bare_root = self.settings.bare_root.resolve()
        candidate = (bare_root / problem["repo_name"]).resolve()
        if bare_root not in candidate.parents or candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("published bare repository is unavailable")
        return candidate

    def published_revision(self, problem_id: int) -> PublishedRevision:
        problem = self.store.problem(int(problem_id))
        if problem is None:
            raise ValueError("problem not found")
        bare_repo = self._bare_repo(problem)
        resolved = run_git(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", "refs/heads/main^{commit}"],
            timeout=120,
        )
        source_commit = resolved.stdout.strip()
        if resolved.returncode != 0 or not source_commit:
            raise ValueError("problem has no published main revision")
        count = run_git(
            ["git", "-C", str(bare_repo), "rev-list", "--count", source_commit],
            timeout=120,
        )
        if count.returncode != 0:
            raise ValueError(count.stderr or "failed to derive Git revision number")
        return PublishedRevision(
            problem=problem,
            source_commit=source_commit,
            revision_number=int(count.stdout.strip()),
            bare_repo=bare_repo,
        )

    def revision_number(self, problem_id: int, source_commit: str) -> int | None:
        problem = self.store.problem(int(problem_id))
        if problem is None:
            return None
        bare_repo = self._bare_repo(problem)
        reachable = run_git(
            ["git", "-C", str(bare_repo), "merge-base", "--is-ancestor", source_commit, "refs/heads/main"],
            timeout=120,
        )
        if reachable.returncode != 0:
            return None
        count = run_git(["git", "-C", str(bare_repo), "rev-list", "--count", source_commit], timeout=120)
        if count.returncode != 0:
            return None
        return int(count.stdout.strip())

    @staticmethod
    def _statement_languages(bare_repo: Path, source_commit: str) -> list[str]:
        result = run_git(
            ["git", "-C", str(bare_repo), "ls-tree", "--name-only", f"{source_commit}:statement-sections"],
            timeout=120,
        )
        if result.returncode != 0:
            return []
        languages = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        priority = {"english": 0, "chinese": 1}
        return sorted(languages, key=lambda language: (priority.get(language, len(priority)), language))

    def published_config(self, revision: PublishedRevision) -> dict[str, object]:
        result = run_git(
            ["git", "-C", str(revision.bare_repo), "show", f"{revision.source_commit}:config/problem.json"],
            timeout=120,
        )
        if result.returncode != 0:
            raise ValueError("published problem config is unavailable")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("published problem config must be an object")
        return payload

    def readiness(self, problem_id: int) -> ProblemReadiness:
        try:
            published = self.published_revision(int(problem_id))
        except (OSError, RuntimeError, ValueError):
            return {
                "problem_id": int(problem_id),
                "published_commit": "",
                "published_revision_number": None,
                "materialized_commit": "",
                "materialized_revision_number": None,
                "materialization_id": "",
                "archive_sha256": "",
                "current_is_materialized": False,
                "statement_languages": [],
                "missing_reason": "no published Git revision",
            }
        materialization = self.latest_available_materialization(int(problem_id))
        if materialization is None:
            return {
                "problem_id": int(problem_id),
                "published_commit": published.source_commit,
                "published_revision_number": published.revision_number,
                "materialized_commit": "",
                "materialized_revision_number": None,
                "materialization_id": "",
                "archive_sha256": "",
                "current_is_materialized": False,
                "statement_languages": self._statement_languages(
                    published.bare_repo, published.source_commit
                ),
                "missing_reason": "no complete Native materialization; Export this problem first",
            }
        return {
            "problem_id": int(problem_id),
            "published_commit": published.source_commit,
            "published_revision_number": published.revision_number,
            "materialized_commit": materialization["source_commit"],
            "materialized_revision_number": materialization["revision_number"],
            "materialization_id": materialization["id"],
            "archive_sha256": materialization["archive_sha256"],
            "current_is_materialized": materialization["source_commit"] == published.source_commit,
            "statement_languages": self._statement_languages(published.bare_repo, materialization["source_commit"]),
            "missing_reason": "",
        }

    def _archive_path(self, row: MaterializationRow) -> Path:
        root = self.settings.artifacts_root.resolve()
        path = (root / Path(*PurePosixPath(row["archive_rel_path"]).parts)).resolve()
        if root not in path.parents:
            raise ValueError("materialization archive path escapes artifacts root")
        return path

    def available_materialization(
        self, problem_id: int, source_commit: str
    ) -> MaterializationRow | None:
        row = self.store.materialization_for_revision(
            int(problem_id), source_commit
        )
        if row is None or row["status"] != "available":
            return None
        try:
            path = self._archive_path(row)
            if path.is_symlink() or not path.is_file():
                raise ValueError("Native archive is missing")
            if int(path.stat().st_size) != row["archive_size_bytes"]:
                raise ValueError("Native archive size changed")
            if sha256_file(path) != row["archive_sha256"]:
                raise ValueError("Native archive checksum changed")
        except Exception as exc:
            self.store.mark_unavailable(row["id"], str(exc))
            return None
        return row

    def latest_available_materialization(self, problem_id: int) -> MaterializationRow | None:
        for row in self.store.available_revisions(int(problem_id)):
            available = self.available_materialization(int(problem_id), row["source_commit"])
            if available is not None:
                return available
        return None

    @staticmethod
    def _copy_source_tree(source: Path, target: Path) -> None:
        if (source / "test_data").exists():
            raise ValueError("published source must not contain test_data")
        source_root = source.resolve()
        for dirpath, dirnames, filenames in os.walk(source_root, topdown=True, followlinks=False):
            parent = Path(dirpath)
            rel_parent = parent.relative_to(source_root)
            destination_parent = target / rel_parent
            destination_parent.mkdir(parents=True, exist_ok=True)
            shutil.copystat(parent, destination_parent, follow_symlinks=False)
            dirnames[:] = sorted(dirnames)
            for dirname in dirnames:
                directory = parent / dirname
                if directory.is_symlink():
                    raise ValueError(
                        "published source contains a symbolic link: "
                        f"{directory.relative_to(source_root)}"
                    )
                if not directory.is_dir():
                    raise ValueError(f"published source contains a special file: {directory.relative_to(source_root)}")
            for filename in sorted(filenames):
                item = parent / filename
                if item.is_symlink() or not item.is_file():
                    raise ValueError(f"published source contains a non-regular file: {item.relative_to(source_root)}")
                destination = destination_parent / filename
                shutil.copy2(item, destination)

    def _verification_payload(self, verification_id: str, test_id: str, key: str) -> PayloadFile | None:
        ref = self.store.artifact_ref(verification_id, test_id, key)
        return None if not ref else self._artifact_file_resolver(ref)

    @staticmethod
    def _write_payload(source: Path, target: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"materialization payload is unavailable: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    @staticmethod
    def _different_text(payload: str, judge_path: Path | None) -> bool:
        return judge_path is None or payload.encode("utf-8") != judge_path.read_bytes()

    def _materialize_tests(
        self,
        *,
        snapshot: Path,
        package_root: Path,
        verification_id: str,
        mode: str,
    ) -> list[NativeTestEntry]:
        tests = load_tests_spec(snapshot / "tests" / "spec.json")
        if not tests:
            raise ValueError("Native materialization requires tests/spec.json entries")
        manifest_tests: list[NativeTestEntry] = []
        for row in tests:
            test_id = str(row["id"])
            if Path(test_id).name != test_id or test_id in {"", ".", ".."}:
                raise ValueError(f"test ID is not package-safe: {test_id}")
            test_root = package_root / "test_data" / "tests" / test_id
            input_path = test_root / "input"
            payload = self._verification_payload(verification_id, test_id, "input_ref")
            if payload is None:
                raise ValueError(f"verification input is missing: {test_id}")
            self._write_payload(payload.path, input_path)
            item: NativeTestEntry = {
                "id": test_id,
                "kind": str(row["kind"]),
                "sample": bool(row["sample"]),
                "input": describe_file(input_path, root=package_root),
            }
            answer_path: Path | None = None
            answer_payload = self._verification_payload(verification_id, test_id, "answer_ref")
            if answer_payload is not None:
                answer_path = test_root / "answer"
                self._write_payload(answer_payload.path, answer_path)
                item["answer"] = describe_file(answer_path, root=package_root)
            elif mode != "interactive":
                raise ValueError(f"verification answer is missing: {test_id}")
            sample_input = str(row["sample_input"] or "")
            if bool(row["sample"]) and sample_input and self._different_text(sample_input, input_path):
                path = test_root / "sample-input"
                path.write_text(sample_input, encoding="utf-8", newline="")
                item["sample_input"] = describe_file(path, root=package_root)
            sample_output = str(row["sample_output"] or "")
            if bool(row["sample"]) and sample_output and self._different_text(sample_output, answer_path):
                path = test_root / "sample-output"
                path.write_text(sample_output, encoding="utf-8", newline="")
                item["sample_output"] = describe_file(path, root=package_root)
            manifest_tests.append(item)
        return manifest_tests

    @staticmethod
    def _mode(snapshot: Path) -> tuple[str, int]:
        config_path = snapshot / "config" / "problem.json"
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        mode = str(payload.get("mode") or "pass-fail")
        pass_limit = int(payload.get("pass_limit") or 1)
        if mode not in {"pass-fail", "interactive"} or pass_limit < 1:
            raise ValueError("published problem has invalid mode/pass_limit")
        return mode, pass_limit

    @staticmethod
    def _write_archive(source_root: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for dirpath, dirnames, filenames in os.walk(source_root, topdown=True, followlinks=False):
                parent = Path(dirpath)
                dirnames[:] = sorted(dirnames)
                rel_parent = parent.relative_to(source_root)
                if rel_parent.parts:
                    archive.writestr(rel_parent.as_posix().rstrip("/") + "/", b"")
                for filename in sorted(filenames):
                    source = parent / filename
                    if source.is_symlink() or not source.is_file():
                        raise ValueError(f"Native archive source is not regular: {source}")
                    archive.write(source, source.relative_to(source_root).as_posix())

    def _build_native(
        self,
        *,
        revision: PublishedRevision,
        snapshot: Path,
        verification_id: str,
        build_id: str,
    ) -> MaterializationRow:
        self.store.mark_build_phase(build_id, "native")
        existing = self.store.materialization_for_revision(
            int(revision.problem["id"]), revision.source_commit
        )
        materialization_id = existing["id"] if existing is not None else f"pm-{uuid.uuid4().hex}"
        staging = self.settings.artifacts_root / ".staging" / materialization_id
        package_root = staging / "package"
        archive_partial = staging / "native.zip.partial"
        final_archive = (
            self.settings.artifacts_root
            / "materializations"
            / str(revision.problem["id"])
            / revision.source_commit
            / "native.zip"
        )
        shutil.rmtree(staging, ignore_errors=True)
        package_root.mkdir(parents=True, exist_ok=True)
        try:
            digest = source_digest(snapshot)
            self._copy_source_tree(snapshot, package_root)
            mode, pass_limit = self._mode(package_root)
            tests = self._materialize_tests(
                snapshot=snapshot,
                package_root=package_root,
                verification_id=verification_id,
                mode=mode,
            )
            manifest: NativeManifest = {
                "source_commit": revision.source_commit,
                "revision_number": revision.revision_number,
                "source_digest": digest,
                "mode": mode,
                "pass_limit": pass_limit,
                "verification": {"id": verification_id, "source": "full-verification"},
                "tests": tests,
            }
            manifest_path = package_root / "test_data" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(dumps_manifest(manifest), encoding="utf-8", newline="\n")
            validate_manifest_files(package_root, manifest)
            self._write_archive(package_root, archive_partial)
            final_archive.parent.mkdir(parents=True, exist_ok=True)
            os.replace(archive_partial, final_archive)
            now = now_iso()
            row: MaterializationRow = {
                "id": materialization_id,
                "problem_id": int(revision.problem["id"]),
                "source_commit": revision.source_commit,
                "revision_number": revision.revision_number,
                "source_digest": digest,
                "archive_rel_path": final_archive.relative_to(self.settings.artifacts_root).as_posix(),
                "archive_sha256": sha256_file(final_archive),
                "archive_size_bytes": int(final_archive.stat().st_size),
                "verification_id": verification_id,
                "status": "available",
                "created_at": now,
                "checked_at": now,
                "unavailable_reason": "",
            }
            self.store.insert_materialization(row, build_id=build_id)
            return row
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def ensure_materialization(
        self,
        revision: PublishedRevision,
        verification_builder: VerificationBuilder,
    ) -> MaterializationRow:
        lock = self._build_lock(int(revision.problem["id"]), revision.source_commit)
        with lock:
            existing = self.available_materialization(int(revision.problem["id"]), revision.source_commit)
            if existing is not None:
                return existing
            build_id = f"pb-{uuid.uuid4().hex}"
            verification_id = f"pv-{uuid.uuid4().hex}"
            build = self.store.create_or_retry_build(
                build_id=build_id,
                problem_id=int(revision.problem["id"]),
                source_commit=revision.source_commit,
                verification_id=verification_id,
            )
            build_id = build["id"]
            verification_id = build["verification_id"] or verification_id
            self.store.mark_build_running(build_id, phase="snapshot")
            snapshot_parent = self.settings.artifacts_root / ".staging" / f"snapshot-{uuid.uuid4().hex}"
            snapshot = snapshot_parent / "source"
            try:
                extract_git_archive(revision.bare_repo, revision.source_commit, snapshot, timeout=120)
                self.store.mark_build_phase(build_id, "verification")
                completed_verification_id = verification_builder(
                    snapshot, revision.source_commit, revision.revision_number, verification_id
                )
                if completed_verification_id != verification_id:
                    raise RuntimeError("materialization verification identity changed")
                return self._build_native(
                    revision=revision,
                    snapshot=snapshot,
                    verification_id=verification_id,
                    build_id=build_id,
                )
            except Exception as exc:
                self.store.mark_build_failed(build_id, str(exc))
                raise
            finally:
                shutil.rmtree(snapshot_parent, ignore_errors=True)

    @staticmethod
    def _safe_extract(archive_path: Path, destination: Path) -> None:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                rel = PurePosixPath(info.filename)
                if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
                    raise ValueError("Native archive contains an unsafe path")
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise ValueError("Native archive contains a symbolic link")
                file_type = stat.S_IFMT(mode)
                if file_type and not (stat.S_ISDIR(mode) if info.is_dir() else stat.S_ISREG(mode)):
                    raise ValueError("Native archive contains a special file")
                target = destination / Path(*rel.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    if mode:
                        target.chmod(stat.S_IMODE(mode))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                if mode:
                    target.chmod(stat.S_IMODE(mode))

    @contextmanager
    def open_reader(self, materialization_id: str) -> Iterator[NativePackageReader]:
        row = self.store.materialization(materialization_id)
        if row is None or row["status"] != "available":
            raise ValueError("Native materialization is unavailable")
        archive_path = self._archive_path(row)
        if self.available_materialization(row["problem_id"], row["source_commit"]) is None:
            raise ValueError("Native materialization is unavailable")
        extraction = self.settings.artifacts_root / ".staging" / f"read-{uuid.uuid4().hex}"
        extraction.mkdir(parents=True, exist_ok=False)
        try:
            try:
                self._safe_extract(archive_path, extraction)
                manifest_path = extraction / "test_data" / "manifest.json"
                manifest = load_manifest(manifest_path)
                if manifest["source_commit"] != row["source_commit"]:
                    raise ValueError("Native manifest commit does not match materialization")
                validate_manifest_files(extraction, manifest)
            except Exception as exc:
                self.store.mark_unavailable(row["id"], str(exc))
                raise
            yield NativePackageReader(materialization=row, root=extraction, manifest=manifest)
        finally:
            shutil.rmtree(extraction, ignore_errors=True)

    def native_archive(self, materialization_id: str) -> tuple[MaterializationRow, Path]:
        row = self.store.materialization(materialization_id)
        if row is None:
            raise ValueError("Native materialization not found")
        available = self.available_materialization(row["problem_id"], row["source_commit"])
        if available is None or available["id"] != materialization_id:
            raise ValueError("Native materialization is unavailable")
        return available, self._archive_path(available)

    def fail_interrupted_builds(self) -> int:
        staging = self.settings.artifacts_root / ".staging"
        if staging.exists() and staging.is_dir() and not staging.is_symlink():
            for child in staging.iterdir():
                if child.is_symlink():
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file():
                    child.unlink(missing_ok=True)
        for row in self.store.all_available_materializations():
            self.available_materialization(row["problem_id"], row["source_commit"])
        return self.store.fail_interrupted_builds()
