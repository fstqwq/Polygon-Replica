"""Build and read immutable problem packages from published Git revisions.

This module owns the only bridge from a bare repository revision to a package.
Consumers receive a validated package reader and never a workspace, Git handle, or
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
from typing import Literal, TypedDict

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


class FrozenMaterializationMismatch(ValueError):
    """A frozen consumer no longer points at the recorded Native archive."""


class MaterializationOperationBusy(RuntimeError):
    """The revision is already being validated, read, or rebuilt."""


PublishedPackageStatus = Literal["ready", "stale", "none"]


class PublishedPackageReadiness(TypedDict):
    problem_id: int
    published_commit: str
    published_revision_number: int | None
    materialized_revision_number: int | None
    materialization_id: str
    status: PublishedPackageStatus
    missing_reason: str


class ProblemPackageService:
    """Materialize published revisions into verified, replaceable Native caches."""

    def __init__(
        self,
        db: DB,
        settings: Settings,
        artifact_file_resolver: Callable[[str], PayloadFile | None],
        verification_id_allocator: Callable[[], str],
    ) -> None:
        self.db = db
        self.settings = settings
        self.store = ProblemPackageStore(db)
        self._artifact_file_resolver = artifact_file_resolver
        self._verification_id_allocator = verification_id_allocator
        self._locks_guard = threading.Lock()
        self._build_locks: dict[tuple[int, str], threading.RLock] = {}

    def _build_lock(self, problem_id: int, source_commit: str) -> threading.RLock:
        key = (int(problem_id), source_commit)
        with self._locks_guard:
            return self._build_locks.setdefault(key, threading.RLock())

    @contextmanager
    def _revision_operation(
        self,
        problem_id: int,
        source_commit: str,
    ) -> Iterator[None]:
        lock = self._build_lock(int(problem_id), source_commit)
        if not lock.acquire(blocking=False):
            raise MaterializationOperationBusy(
                "Native materialization operation already running"
            )
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def materialization_operation(
        self,
        materialization_id: str,
    ) -> Iterator[MaterializationRow]:
        row = self.store.materialization(materialization_id)
        if row is None:
            raise ValueError("Native materialization not found")
        with self._revision_operation(row["problem_id"], row["source_commit"]):
            current = self.store.materialization(materialization_id)
            if current is None:
                raise ValueError("Native materialization not found")
            yield current

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
        return self._published_revision_for_problem(problem)

    def _published_revision_for_problem(
        self,
        problem: PublishedProblem,
    ) -> PublishedRevision:
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

    def published_revisions_many(
        self,
        problem_ids: list[int],
    ) -> tuple[dict[int, PublishedRevision], dict[int, str]]:
        ids = list(dict.fromkeys(int(problem_id) for problem_id in problem_ids))
        problems = self.store.problems(ids)
        revisions: dict[int, PublishedRevision] = {}
        errors: dict[int, str] = {}
        for problem_id in ids:
            problem = problems.get(problem_id)
            if problem is None:
                errors[problem_id] = "problem not found"
                continue
            try:
                revisions[problem_id] = self._published_revision_for_problem(problem)
            except (OSError, RuntimeError, ValueError):
                errors[problem_id] = "no published Git revision"
        return revisions, errors

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

    def published_revision_at(
        self,
        problem_id: int,
        source_commit: str,
        revision_number: int,
    ) -> PublishedRevision:
        problem = self.store.problem(int(problem_id))
        if problem is None:
            raise ValueError("problem not found")
        bare_repo = self._bare_repo(problem)
        resolved = run_git(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", f"{source_commit}^{{commit}}"],
            timeout=120,
        )
        commit = resolved.stdout.strip()
        if resolved.returncode != 0 or commit != source_commit:
            raise ValueError("frozen published revision is unavailable")
        derived_revision = self.revision_number(int(problem_id), source_commit)
        if derived_revision != int(revision_number):
            raise ValueError("frozen Git revision number changed")
        return PublishedRevision(
            problem=problem,
            source_commit=source_commit,
            revision_number=int(revision_number),
            bare_repo=bare_repo,
        )

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

    @staticmethod
    def _published_readiness(
        problem_id: int,
        published: PublishedRevision | None,
        materialization: MaterializationRow | None,
        previous_materialization: MaterializationRow | None,
        error: str = "",
    ) -> PublishedPackageReadiness:
        if published is None:
            return {
                "problem_id": problem_id,
                "published_commit": "",
                "published_revision_number": None,
                "materialized_revision_number": None,
                "materialization_id": "",
                "status": "none",
                "missing_reason": error or "no published Git revision",
            }
        if materialization is not None and materialization["status"] == "available":
            return {
                "problem_id": problem_id,
                "published_commit": published.source_commit,
                "published_revision_number": published.revision_number,
                "materialized_revision_number": materialization["revision_number"],
                "materialization_id": materialization["id"],
                "status": "ready",
                "missing_reason": "",
            }
        if previous_materialization is not None:
            return {
                "problem_id": problem_id,
                "published_commit": published.source_commit,
                "published_revision_number": published.revision_number,
                "materialized_revision_number": previous_materialization["revision_number"],
                "materialization_id": previous_materialization["id"],
                "status": "stale",
                "missing_reason": "A newer revision has not been packaged",
            }
        return {
            "problem_id": problem_id,
            "published_commit": published.source_commit,
            "published_revision_number": published.revision_number,
            "materialized_revision_number": None,
            "materialization_id": "",
            "status": "none",
            "missing_reason": (
                "Package unavailable; rebuild required"
                if materialization is not None
                else "Package not built"
            ),
        }

    def published_readiness(
        self,
        problem_id: int,
    ) -> PublishedPackageReadiness:
        return self.published_readiness_many([int(problem_id)])[int(problem_id)]

    def published_readiness_many(
        self,
        problem_ids: list[int],
    ) -> dict[int, PublishedPackageReadiness]:
        ids = list(dict.fromkeys(int(problem_id) for problem_id in problem_ids))
        revisions, errors = self.published_revisions_many(ids)
        materializations = self.store.materializations_for_revisions(
            [
                (problem_id, revision.source_commit)
                for problem_id, revision in revisions.items()
            ]
        )
        previous_materializations = self.store.latest_available_materializations_before(
            [
                (problem_id, revision.revision_number)
                for problem_id, revision in revisions.items()
            ]
        )
        return {
            problem_id: self._published_readiness(
                problem_id,
                revisions.get(problem_id),
                (
                    materializations.get(
                        (problem_id, revisions[problem_id].source_commit)
                    )
                    if problem_id in revisions
                    else None
                ),
                previous_materializations.get(problem_id),
                errors.get(problem_id, ""),
            )
            for problem_id in ids
        }

    def _archive_path(self, row: MaterializationRow) -> Path:
        root = self.settings.artifacts_root.resolve()
        candidate = root / Path(*PurePosixPath(row["archive_rel_path"]).parts)
        if candidate.is_symlink():
            raise ValueError("materialization archive must not be a symbolic link")
        path = candidate.resolve()
        if root not in path.parents:
            raise ValueError("materialization archive path escapes artifacts root")
        return path

    def _remove_artifact_file(self, rel_path: str) -> str:
        try:
            root = self.settings.artifacts_root.resolve()
            path = root / Path(*PurePosixPath(rel_path).parts)
            resolved_parent = path.parent.resolve()
            if root != resolved_parent and root not in resolved_parent.parents:
                raise ValueError("artifact path escapes artifacts root")
            if path.is_symlink() or path.is_file():
                path.unlink()
            return ""
        except Exception as exc:
            return str(exc)

    def _invalidate_materialization(
        self,
        row: MaterializationRow,
        reason: str,
    ) -> str:
        exports = self.store.invalidate_materialization(row["id"], reason)
        cleanup_errors: list[str] = []
        archive_error = self._remove_artifact_file(row["archive_rel_path"])
        if archive_error:
            cleanup_errors.append(archive_error)
        for export in exports:
            if export["export_type"] == "native":
                continue
            export_error = self._remove_artifact_file(export["archive_rel_path"])
            if export_error:
                cleanup_errors.append(export_error)
        return "; ".join(cleanup_errors)

    def available_materialization(
        self, problem_id: int, source_commit: str
    ) -> MaterializationRow | None:
        initial = self.store.materialization_for_revision(
            int(problem_id), source_commit
        )
        if initial is None or initial["status"] != "available":
            return None
        row = initial
        try:
            with self._revision_operation(int(problem_id), source_commit):
                row = self.store.materialization_for_revision(
                    int(problem_id), source_commit
                )
                if row is None or row["status"] != "available":
                    return None
                self._validate_materialization(row)
        except MaterializationOperationBusy:
            return None
        except Exception as exc:
            self._invalidate_materialization(row, str(exc))
            return None
        return row

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
        tests_spec_max_bytes: int,
    ) -> list[NativeTestEntry]:
        tests = load_tests_spec(
            snapshot / "tests" / "spec.json",
            max_bytes=tests_spec_max_bytes,
        )
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
        invalidate_exports: bool,
    ) -> MaterializationRow:
        config_snapshot = self.db.config_values.snapshot()
        tests_spec_max_bytes = int(config_snapshot["TEXTAREA_MAX_BYTES"])
        self.store.mark_build_phase(build_id, "native")
        existing = self.store.materialization_for_revision(
            int(revision.problem["id"]), revision.source_commit
        )
        materialization_id = existing["id"] if existing is not None else f"pm-{uuid.uuid4().hex}"
        staging = self.settings.artifacts_root / ".staging" / materialization_id
        package_root = staging / "package"
        archive_partial = staging / "native.zip.partial"
        previous_archive = staging / "previous-native.zip"
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
                tests_spec_max_bytes=tests_spec_max_bytes,
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
            validate_manifest_files(
                package_root,
                manifest,
                tests_spec_max_bytes=tests_spec_max_bytes,
            )
            self._write_archive(package_root, archive_partial)
            now = now_iso()
            staged_row: MaterializationRow = {
                "id": materialization_id,
                "problem_id": int(revision.problem["id"]),
                "source_commit": revision.source_commit,
                "revision_number": revision.revision_number,
                "source_digest": digest,
                "archive_rel_path": archive_partial.relative_to(self.settings.artifacts_root).as_posix(),
                "archive_sha256": sha256_file(archive_partial),
                "archive_size_bytes": int(archive_partial.stat().st_size),
                "verification_id": verification_id,
                "status": "available",
                "created_at": now,
                "checked_at": now,
                "unavailable_reason": "",
            }
            self._validate_materialization(staged_row, check_current=False)
            row: MaterializationRow = {
                **staged_row,
                "archive_rel_path": final_archive.relative_to(
                    self.settings.artifacts_root
                ).as_posix(),
            }
            final_archive.parent.mkdir(parents=True, exist_ok=True)
            had_previous_archive = final_archive.is_symlink() or final_archive.exists()
            published_archive = False
            try:
                if had_previous_archive:
                    os.replace(final_archive, previous_archive)
                os.replace(archive_partial, final_archive)
                published_archive = True
                invalidated = self.store.insert_materialization(
                    row,
                    build_id=build_id,
                    invalidate_exports=invalidate_exports,
                )
            except Exception:
                if published_archive and (
                    final_archive.is_symlink() or final_archive.is_file()
                ):
                    final_archive.unlink()
                if had_previous_archive:
                    os.replace(previous_archive, final_archive)
                raise
            if had_previous_archive and (
                previous_archive.is_symlink() or previous_archive.is_file()
            ):
                previous_archive.unlink()
            for export in invalidated:
                if export["export_type"] != "native":
                    self._remove_artifact_file(export["archive_rel_path"])
            return row
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _run_materialization_build(
        self,
        revision: PublishedRevision,
        verification_builder: VerificationBuilder,
        *,
        invalidate_exports: bool,
    ) -> MaterializationRow:
        build_id = f"pb-{uuid.uuid4().hex}"
        verification_id = self._verification_id_allocator()
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
                invalidate_exports=invalidate_exports,
            )
        except Exception as exc:
            self.store.mark_build_failed(build_id, str(exc))
            raise
        finally:
            shutil.rmtree(snapshot_parent, ignore_errors=True)

    def ensure_materialization(
        self,
        revision: PublishedRevision,
        verification_builder: VerificationBuilder,
    ) -> MaterializationRow:
        problem_id = int(revision.problem["id"])
        with self._revision_operation(problem_id, revision.source_commit):
            existing = self.store.materialization_for_revision(
                problem_id,
                revision.source_commit,
            )
            if existing is not None:
                if existing["status"] != "available":
                    raise ValueError(
                        "Native materialization is unavailable; explicit rebuild required"
                    )
                try:
                    self._validate_materialization(existing)
                except Exception as exc:
                    cleanup_error = self._invalidate_materialization(existing, str(exc))
                    detail = f"; cleanup failed: {cleanup_error}" if cleanup_error else ""
                    raise ValueError(
                        f"Native materialization integrity check failed: {exc}{detail}"
                    ) from exc
                return existing
            return self._run_materialization_build(
                revision,
                verification_builder,
                invalidate_exports=False,
            )

    def rebuild_materialization(
        self,
        revision: PublishedRevision,
        verification_builder: VerificationBuilder,
    ) -> MaterializationRow:
        problem_id = int(revision.problem["id"])
        with self._revision_operation(problem_id, revision.source_commit):
            existing = self.store.materialization_for_revision(
                problem_id,
                revision.source_commit,
            )
            return self._run_materialization_build(
                revision,
                verification_builder,
                invalidate_exports=existing is not None,
            )

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
    def _validated_reader(
        self,
        row: MaterializationRow,
        *,
        expected_archive_sha256: str | None = None,
        check_current: bool = True,
    ) -> Iterator[NativePackageReader]:
        if (
            expected_archive_sha256 is not None
            and row["archive_sha256"] != expected_archive_sha256
        ):
            raise FrozenMaterializationMismatch(
                "frozen Native archive checksum no longer matches"
            )
        archive_path = self._archive_path(row)
        if archive_path.is_symlink() or not archive_path.is_file():
            raise ValueError("Native archive is missing")
        if int(archive_path.stat().st_size) != row["archive_size_bytes"]:
            raise ValueError("Native archive size changed")
        if sha256_file(archive_path) != row["archive_sha256"]:
            raise ValueError("Native archive checksum changed")
        extraction = self.settings.artifacts_root / ".staging" / f"read-{uuid.uuid4().hex}"
        extraction.mkdir(parents=True, exist_ok=False)
        try:
            self._safe_extract(archive_path, extraction)
            manifest_path = extraction / "test_data" / "manifest.json"
            manifest = load_manifest(manifest_path)
            if manifest["source_commit"] != row["source_commit"]:
                raise ValueError("Native manifest commit does not match materialization")
            if manifest["revision_number"] != row["revision_number"]:
                raise ValueError("Native manifest revision does not match materialization")
            if manifest["source_digest"] != row["source_digest"]:
                raise ValueError("Native manifest source digest does not match materialization")
            if manifest["verification"]["id"] != row["verification_id"]:
                raise ValueError(
                    "Native manifest verification does not match materialization"
                )
            validate_manifest_files(
                extraction,
                manifest,
                tests_spec_max_bytes=int(
                    self.db.config_values.snapshot()["TEXTAREA_MAX_BYTES"]
                ),
            )
            yield NativePackageReader(
                materialization=row,
                root=extraction,
                manifest=manifest,
            )
            if check_current:
                current = self.store.materialization(row["id"])
                if (
                    current is None
                    or current["status"] != "available"
                    or current["archive_sha256"] != row["archive_sha256"]
                ):
                    raise FrozenMaterializationMismatch(
                        "Native materialization changed while it was being read"
                    )
                if (
                    expected_archive_sha256 is not None
                    and current["archive_sha256"] != expected_archive_sha256
                ):
                    raise FrozenMaterializationMismatch(
                        "frozen Native archive checksum no longer matches"
                    )
        finally:
            shutil.rmtree(extraction, ignore_errors=True)

    def _validate_materialization(
        self,
        row: MaterializationRow,
        *,
        expected_archive_sha256: str | None = None,
        check_current: bool = True,
    ) -> None:
        with self._validated_reader(
            row,
            expected_archive_sha256=expected_archive_sha256,
            check_current=check_current,
        ):
            pass

    @contextmanager
    def open_reader(
        self,
        materialization_id: str,
        *,
        expected_archive_sha256: str | None = None,
    ) -> Iterator[NativePackageReader]:
        with self.materialization_operation(materialization_id) as row:
            if row["status"] != "available":
                raise ValueError("Native materialization is unavailable; explicit rebuild required")
            validation = self._validated_reader(
                row,
                expected_archive_sha256=expected_archive_sha256,
            )
            try:
                reader = validation.__enter__()
            except FrozenMaterializationMismatch:
                raise
            except Exception as exc:
                cleanup_error = self._invalidate_materialization(row, str(exc))
                detail = f"; cleanup failed: {cleanup_error}" if cleanup_error else ""
                raise ValueError(f"Native materialization integrity check failed: {exc}{detail}") from exc
            try:
                yield reader
            except BaseException as exc:
                validation.__exit__(type(exc), exc, exc.__traceback__)
                raise
            else:
                validation.__exit__(None, None, None)

    def native_archive(
        self,
        materialization_id: str,
        *,
        expected_archive_sha256: str | None = None,
    ) -> tuple[MaterializationRow, Path]:
        with self.materialization_operation(materialization_id) as row:
            if row["status"] != "available":
                raise ValueError("Native materialization is unavailable; explicit rebuild required")
            try:
                self._validate_materialization(
                    row,
                    expected_archive_sha256=expected_archive_sha256,
                )
            except FrozenMaterializationMismatch:
                raise
            except Exception as exc:
                cleanup_error = self._invalidate_materialization(row, str(exc))
                detail = f"; cleanup failed: {cleanup_error}" if cleanup_error else ""
                raise ValueError(f"Native materialization integrity check failed: {exc}{detail}") from exc
            return row, self._archive_path(row)

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
