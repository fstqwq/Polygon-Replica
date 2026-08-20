"""Build and read Native Package archives from published Git revisions.

This module owns the only bridge from a bare repository revision to a package.
Consumers receive a validated package reader and never a workspace, Git handle, or
verification artifact reference.
"""

import os
import shutil
import stat
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, TypedDict

from app.db import DB, now_iso
from app.main_util import problem_slug_leaf
from app.service.execution.codec import execution_result_from_json
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.fs.op import extract_git_archive
from app.service.platform.git_process import run_git
from app.service.platform.hashing import sha256_file
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.problem.runtime_config import (
    ProblemConfig,
    parse_problem_config,
    problem_config_limits,
)
from app.service.problem.source_tree import ProblemSourceTree, load_problem_source_tree
from app.service.problem_package.manifest import (
    NativePackageFileEntry,
    NativePackageManifest,
    NativePackageSolutionEntry,
    NativePackageTestEntry,
    describe_file,
    dumps_manifest,
    load_manifest,
    source_digest,
    validate_manifest_files,
)
from app.service.problem_package.layout import (
    PACKAGE_DERIVED_ROOT_NAMES,
    STATEMENT_BUILD_DIR,
    TEST_DATA_DIR,
)
from app.service.problem_package.store import (
    MaterializationRow,
    NativePackageTestExecutionRow,
    ProblemPackageStore,
    PublishedProblem,
)
from app.service.repository.revision import parse_verification_source
from app.service.statement.context import normalize_statement_language, statement_languages
from app.service.statement.examples import StatementExamplesProducer
from app.service.statement.render import render_statement_offline_tree
from app.service.verification.result_match import run_verdict_short
from app.service.verification.types import Kind, VerificationStatus


VerificationBuilder = Callable[[Path, str, int, str], str]
NativePackage = MaterializationRow


@dataclass
class NativePackageDownload:
    native_package: NativePackage
    stream: BinaryIO
    operation: AbstractContextManager[NativePackage]
    closed: bool = False

    def chunks(self) -> Iterator[bytes]:
        try:
            while chunk := self.stream.read(1024 * 1024):
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.stream.close()
        finally:
            self.operation.__exit__(None, None, None)


@dataclass(frozen=True)
class PublishedRevision:
    problem: PublishedProblem
    source_commit: str
    revision_number: int
    bare_repo: Path


@dataclass(frozen=True)
class VerificationTestOwner:
    source_id: str
    test_name: str
    input_ref: str


@dataclass(frozen=True)
class VerificationTestOwners:
    by_source_id: dict[str, VerificationTestOwner]
    by_test_name: dict[str, VerificationTestOwner]


@dataclass(frozen=True)
class NativePackageReader:
    native_package: NativePackage
    root: Path
    manifest: NativePackageManifest

    def payload(
        self,
        test: NativePackageTestEntry,
        key: Literal["input", "answer", "sample_input", "sample_output"],
    ) -> Path | None:
        descriptor = test.get(key)
        if descriptor is None:
            return None
        return self.root / Path(*PurePosixPath(str(descriptor["path"])).parts)


class FrozenNativePackageMismatch(ValueError):
    """A frozen consumer no longer points at the recorded Native Package."""


class NativePackageOperationBusy(RuntimeError):
    """The revision is already being validated, read, or rebuilt."""


NativePackageStatus = Literal["ready", "queued", "stale", "none"]


class NativePackageReadiness(TypedDict):
    problem_id: int
    published_commit: str
    published_revision_number: int | None
    native_package_revision_number: int | None
    native_package_id: str
    status: NativePackageStatus
    verified: bool
    missing_reason: str


class ProblemPackageService:
    """Prepare and validate reusable Native Packages."""

    def __init__(
        self,
        db: DB,
        storage_layout: StorageLayout,
        artifact_file_resolver: Callable[[str], PayloadFile | None],
        verification_id_allocator: Callable[[], str],
        statement_examples_producer: StatementExamplesProducer,
    ) -> None:
        self.db = db
        self.storage_layout = storage_layout
        self.store = ProblemPackageStore(db)
        self._artifact_file_resolver = artifact_file_resolver
        self._verification_id_allocator = verification_id_allocator
        self._statement_examples_producer = statement_examples_producer
        self._locks_guard = threading.Lock()
        self._build_locks: dict[tuple[int, str], threading.Lock] = {}

    def _build_lock(self, problem_id: int, source_commit: str) -> threading.Lock:
        key = (int(problem_id), source_commit)
        with self._locks_guard:
            return self._build_locks.setdefault(key, threading.Lock())

    @contextmanager
    def _revision_operation(
        self,
        problem_id: int,
        source_commit: str,
    ) -> Iterator[None]:
        lock = self._build_lock(int(problem_id), source_commit)
        if not lock.acquire(blocking=False):
            raise NativePackageOperationBusy(
                "Native Package operation already running"
            )
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def native_package_operation(
        self,
        native_package_id: str,
    ) -> Iterator[NativePackage]:
        row = self.store.materialization(native_package_id)
        if row is None:
            raise ValueError("Native Package not found")
        with self._revision_operation(row["problem_id"], row["source_commit"]):
            current = self.store.materialization(native_package_id)
            if current is None:
                raise ValueError("Native Package not found")
            yield current

    def _bare_repo(self, problem: PublishedProblem) -> Path:
        bare_root = self.storage_layout.bare_root.resolve()
        candidate = self.storage_layout.bare_repository(problem["repo_name"])
        if bare_root not in candidate.parents or candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("published bare repository is unavailable")
        return candidate

    def published_revision(self, problem_id: int) -> PublishedRevision:
        problem = self.store.problem(int(problem_id))
        if problem is None:
            raise ValueError("problem not found")
        return self._published_revision_for_problem(problem)

    def available_native_package_history(
        self,
        problem_id: int,
        *,
        limit: int = 40,
    ) -> list[NativePackage]:
        return self.store.available_native_package_history(
            int(problem_id),
            limit=max(1, int(limit)),
        )

    def native_package(self, native_package_id: str) -> NativePackage | None:
        return self.store.materialization(native_package_id)

    def native_packages_verified_many(
        self,
        native_packages: list[NativePackage],
    ) -> dict[str, bool]:
        certifications = self.store.verification_certifications(
            [row["verification_id"] for row in native_packages]
        )
        result: dict[str, bool] = {}
        for row in native_packages:
            certification = certifications.get(row["verification_id"])
            certification_source = (
                parse_verification_source(certification["source_commit"])
                if certification is not None
                else None
            )
            result[row["id"]] = bool(
                certification is not None
                and certification["problem_id"] == row["problem_id"]
                and certification_source is not None
                and certification_source.base_commit == row["source_commit"]
                and certification["kind"] == Kind.ALL.value
                and certification["status"] == VerificationStatus.OK.value
            )
        return result

    def native_package_verified(self, native_package: NativePackage) -> bool:
        return self.native_packages_verified_many([native_package]).get(
            native_package["id"],
            False,
        )

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

    def published_config(self, revision: PublishedRevision) -> ProblemConfig:
        result = run_git(
            ["git", "-C", str(revision.bare_repo), "show", f"{revision.source_commit}:config/problem.json"],
            timeout=120,
        )
        if result.returncode != 0:
            raise ValueError("published problem config is unavailable")
        return parse_problem_config(
            result.stdout,
            limits=problem_config_limits(self.db.config_values),
        )

    @staticmethod
    def _published_readiness(
        problem_id: int,
        published: PublishedRevision | None,
        materialization: MaterializationRow | None,
        previous_materialization: MaterializationRow | None,
        build_active: bool,
        verified_by_id: dict[str, bool],
        error: str = "",
    ) -> NativePackageReadiness:
        if published is None:
            return {
                "problem_id": problem_id,
                "published_commit": "",
                "published_revision_number": None,
                "native_package_revision_number": None,
                "native_package_id": "",
                "status": "none",
                "verified": False,
                "missing_reason": error or "no published Git revision",
            }
        if materialization is not None and materialization["status"] == "available":
            return {
                "problem_id": problem_id,
                "published_commit": published.source_commit,
                "published_revision_number": published.revision_number,
                "native_package_revision_number": materialization["revision_number"],
                "native_package_id": materialization["id"],
                "status": "ready",
                "verified": verified_by_id.get(materialization["id"], False),
                "missing_reason": "",
            }
        if build_active:
            return {
                "problem_id": problem_id,
                "published_commit": published.source_commit,
                "published_revision_number": published.revision_number,
                "native_package_revision_number": None,
                "native_package_id": "",
                "status": "queued",
                "verified": False,
                "missing_reason": "",
            }
        if previous_materialization is not None:
            return {
                "problem_id": problem_id,
                "published_commit": published.source_commit,
                "published_revision_number": published.revision_number,
                "native_package_revision_number": previous_materialization["revision_number"],
                "native_package_id": previous_materialization["id"],
                "status": "stale",
                "verified": verified_by_id.get(
                    previous_materialization["id"],
                    False,
                ),
                "missing_reason": "A newer revision has no Native Package",
            }
        return {
            "problem_id": problem_id,
            "published_commit": published.source_commit,
            "published_revision_number": published.revision_number,
            "native_package_revision_number": None,
            "native_package_id": "",
            "status": "none",
            "verified": False,
            "missing_reason": (
                "Native Package unavailable; rebuild required"
                if materialization is not None
                else "No Native Package"
            ),
        }

    def published_readiness(
        self,
        problem_id: int,
    ) -> NativePackageReadiness:
        return self.published_readiness_many([int(problem_id)])[int(problem_id)]

    def published_readiness_many(
        self,
        problem_ids: list[int],
    ) -> dict[int, NativePackageReadiness]:
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
        active_builds = self.store.active_builds_for_revisions(
            [
                (problem_id, revision.source_commit)
                for problem_id, revision in revisions.items()
            ]
        )
        readiness_materializations = [
            *materializations.values(),
            *previous_materializations.values(),
        ]
        verified_by_id = self.native_packages_verified_many(
            readiness_materializations
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
                (
                    (problem_id, revisions[problem_id].source_commit)
                    in active_builds
                    if problem_id in revisions
                    else False
                ),
                verified_by_id,
                errors.get(problem_id, ""),
            )
            for problem_id in ids
        }

    def _archive_path(self, row: MaterializationRow) -> Path:
        root = self.storage_layout.artifacts_root.resolve()
        candidate = self.storage_layout.resolve_artifact(row["archive_rel_path"])
        if candidate.is_symlink():
            raise ValueError("Native Package archive must not be a symbolic link")
        path = candidate.resolve()
        if root not in path.parents:
            raise ValueError("Native Package archive path escapes artifacts root")
        return path

    def statement_languages(self, native_package_id: str) -> list[str]:
        """List rendered statement languages without validating or mutating a Package."""
        row = self.store.materialization(native_package_id)
        if row is None or row["status"] != "available":
            return []
        try:
            archive = self._archive_path(row)
            with zipfile.ZipFile(archive, "r") as package:
                names = set(package.namelist())
        except (OSError, ValueError, zipfile.BadZipFile):
            return []
        prefix = f"{STATEMENT_BUILD_DIR}/"
        suffix = "/problem.tex"
        languages: set[str] = set()
        for name in names:
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            middle = name[len(prefix) : -len(suffix)]
            if "/" in middle or not middle:
                continue
            try:
                languages.add(normalize_statement_language(middle))
            except ValueError:
                continue
        return sorted(language for language in languages if language)

    def _remove_artifact_file(self, rel_path: str) -> str:
        try:
            root = self.storage_layout.artifacts_root.resolve()
            path = self.storage_layout.resolve_artifact(rel_path)
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
            export_error = self._remove_artifact_file(export["archive_rel_path"])
            if export_error:
                cleanup_errors.append(export_error)
        return "; ".join(cleanup_errors)

    @staticmethod
    def _copy_source_tree(source: Path, target: Path) -> None:
        if (source / TEST_DATA_DIR).exists():
            raise ValueError("published source must not contain test-data")
        if (source / STATEMENT_BUILD_DIR).exists():
            raise ValueError("published source must not contain statement-build")
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

    def _verification_test_owners(
        self,
        verification_id: str,
        source_tree: ProblemSourceTree,
    ) -> VerificationTestOwners:
        rows = self.store.test_execution_rows(verification_id)
        expected = [
            (str(test["id"]), f"{ordinal:03d}.in", ordinal)
            for ordinal, test in enumerate(source_tree.tests, start=1)
        ]
        actual = [
            (row["source_id"], row["test_name"], row["ordinal"])
            for row in rows
        ]
        if actual != expected:
            raise ValueError("verification generated test results are incomplete")

        owner_by_input_ref: dict[str, NativePackageTestExecutionRow] = {}
        for row in rows:
            if row["final_status"] != "done" or not row["input_ref"]:
                raise ValueError(
                    "verification generated test result is incomplete: "
                    f"{row['test_name']}"
                )
            verdict = run_verdict_short(
                execution_result_from_json(row["result_json"]).verdict.upper()
            )
            if verdict == "SK":
                continue
            if verdict != "AC":
                raise ValueError(
                    "verification generated test result is incomplete: "
                    f"{row['test_name']}"
                )
            if row["input_ref"] in owner_by_input_ref:
                raise ValueError(
                    "verification generated input has multiple owners: "
                    f"{row['test_name']}"
                )
            owner_by_input_ref[row["input_ref"]] = row

        by_source_id: dict[str, VerificationTestOwner] = {}
        by_test_name: dict[str, VerificationTestOwner] = {}
        for row in rows:
            owner_row = owner_by_input_ref.get(row["input_ref"])
            if owner_row is None:
                raise ValueError(
                    "verification generated input owner is missing: "
                    f"{row['test_name']}"
                )
            owner = VerificationTestOwner(
                source_id=owner_row["source_id"],
                test_name=owner_row["test_name"],
                input_ref=owner_row["input_ref"],
            )
            by_source_id[row["source_id"]] = owner
            by_test_name[row["test_name"]] = owner
        return VerificationTestOwners(
            by_source_id=by_source_id,
            by_test_name=by_test_name,
        )

    @staticmethod
    def _write_payload(source: Path, target: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Native Package payload is unavailable: {source}")
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
        source_tree: ProblemSourceTree,
        test_owners: VerificationTestOwners,
    ) -> list[NativePackageTestEntry]:
        tests = source_tree.tests
        if not tests:
            raise ValueError("Native Package requires tests/spec.json entries")
        manifest_tests: list[NativePackageTestEntry] = []
        for row in tests:
            test_id = str(row["id"])
            if Path(test_id).name != test_id or test_id in {"", ".", ".."}:
                raise ValueError(f"test ID is not package-safe: {test_id}")
            owner = test_owners.by_source_id.get(test_id)
            if owner is None:
                raise ValueError(f"verification test owner is missing: {test_id}")
            test_root = package_root / TEST_DATA_DIR / "tests" / test_id
            input_path = test_root / "input"
            payload = self._artifact_file_resolver(owner.input_ref)
            if payload is None:
                raise ValueError(f"verification input is missing: {test_id}")
            self._write_payload(payload.path, input_path)
            item: NativePackageTestEntry = {
                "id": test_id,
                "kind": str(row["kind"]),
                "sample": bool(row["sample"]),
                "input": describe_file(input_path, root=package_root),
            }
            answer_path: Path | None = None
            answer_payload = self._verification_payload(
                verification_id,
                owner.source_id,
                "answer_ref",
            )
            if answer_payload is not None:
                answer_path = test_root / "answer"
                self._write_payload(answer_payload.path, answer_path)
                item["answer"] = describe_file(answer_path, root=package_root)
            elif mode != "interactive":
                raise ValueError(f"verification answer is missing: {test_id}")
            sample_input = row.get("sample_input", "")
            if row["sample"] and sample_input and self._different_text(sample_input, input_path):
                path = test_root / "sample-input"
                path.write_text(sample_input, encoding="utf-8", newline="")
                item["sample_input"] = describe_file(path, root=package_root)
            sample_output = row.get("sample_output", "")
            if row["sample"] and sample_output and self._different_text(sample_output, answer_path):
                path = test_root / "sample-output"
                path.write_text(sample_output, encoding="utf-8", newline="")
                item["sample_output"] = describe_file(path, root=package_root)
            manifest_tests.append(item)
        return manifest_tests

    @staticmethod
    def _manifest_solutions(
        source_tree: ProblemSourceTree,
    ) -> list[NativePackageSolutionEntry]:
        return [
            {
                "source_path": source_path,
                "expected_behavior": source_tree.solution_behaviors[source_path],
            }
            for source_path in sorted(source_tree.solution_behaviors)
        ]

    def _materialize_statement_build(
        self,
        *,
        snapshot: Path,
        render_source: Path,
        package_root: Path,
        problem_title: str,
        verification_id: str,
        test_owners: VerificationTestOwners,
        tests_spec_max_bytes: int,
        statement_sample_max_bytes: int,
    ) -> None:
        self._copy_source_tree(snapshot, render_source)
        examples_bundle = self._statement_examples_producer.produce(
            render_source,
            verification_id=verification_id,
            execution_test_name_by_source_id={
                source_id: owner.test_name
                for source_id, owner in test_owners.by_source_id.items()
            },
            tests_spec_max_bytes=tests_spec_max_bytes,
            statement_sample_max_bytes=statement_sample_max_bytes,
            problem_limits=problem_config_limits(self.db.config_values),
        )
        languages = statement_languages(render_source)
        build_root = package_root / STATEMENT_BUILD_DIR
        for language in languages:
            render_statement_offline_tree(
                render_source,
                language,
                build_root / language,
                problem_title=problem_title,
                examples_bundle=examples_bundle,
                tests_spec_max_bytes=tests_spec_max_bytes,
                statement_sample_max_bytes=statement_sample_max_bytes,
                problem_limits=problem_config_limits(self.db.config_values),
            )

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
                        raise ValueError(
                            f"Native Package archive source is not regular: {source}"
                        )
                    archive.write(source, source.relative_to(source_root).as_posix())

    def _build_native_package(
        self,
        *,
        revision: PublishedRevision,
        snapshot: Path,
        verification_id: str,
        build_id: str,
        invalidate_exports: bool,
        source_tree: ProblemSourceTree,
    ) -> MaterializationRow:
        tests_spec_max_bytes = self.db.config_values.integer("TEXTAREA_MAX_BYTES")
        statement_sample_max_bytes = self.db.config_values.integer(
            "STATEMENT_SAMPLE_MAX_BYTES"
        )
        self.store.mark_build_phase(build_id, "packaging")
        existing = self.store.materialization_for_revision(
            int(revision.problem["id"]), revision.source_commit
        )
        materialization_id = existing["id"] if existing is not None else f"pm-{uuid.uuid4().hex}"
        staging = self.storage_layout.materialization_staging(materialization_id)
        package_root = staging / "package"
        render_source = staging / "statement-source"
        archive_partial = staging / "native-package.zip.partial"
        previous_archive = staging / "previous-native-package.zip"
        final_archive = self.storage_layout.materialization_archive(
            int(revision.problem["id"]),
            revision.source_commit,
        )
        shutil.rmtree(staging, ignore_errors=True)
        package_root.mkdir(parents=True, exist_ok=True)
        try:
            digest = source_digest(snapshot)
            self._copy_source_tree(snapshot, package_root)
            mode = source_tree.problem["mode"]
            pass_limit = source_tree.problem["pass_limit"]
            test_owners = self._verification_test_owners(
                verification_id,
                source_tree,
            )
            tests = self._materialize_tests(
                snapshot=snapshot,
                package_root=package_root,
                verification_id=verification_id,
                mode=mode,
                source_tree=source_tree,
                test_owners=test_owners,
            )
            solutions = self._manifest_solutions(source_tree)
            manifest: NativePackageManifest = {
                "source_commit": revision.source_commit,
                "revision_number": revision.revision_number,
                "source_digest": digest,
                "mode": mode,
                "pass_limit": pass_limit,
                "solutions": solutions,
                "tests": tests,
            }
            manifest_path = package_root / TEST_DATA_DIR / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(dumps_manifest(manifest), encoding="utf-8", newline="\n")
            self._materialize_statement_build(
                snapshot=snapshot,
                render_source=render_source,
                package_root=package_root,
                problem_title=problem_slug_leaf(revision.problem["slug"]),
                verification_id=verification_id,
                test_owners=test_owners,
                tests_spec_max_bytes=tests_spec_max_bytes,
                statement_sample_max_bytes=statement_sample_max_bytes,
            )
            validate_manifest_files(
                package_root,
                manifest,
                tests_spec_max_bytes=tests_spec_max_bytes,
                statement_sample_max_bytes=statement_sample_max_bytes,
            )
            self._write_archive(package_root, archive_partial)
            now = now_iso()
            staged_row: MaterializationRow = {
                "id": materialization_id,
                "problem_id": int(revision.problem["id"]),
                "source_commit": revision.source_commit,
                "revision_number": revision.revision_number,
                "source_digest": digest,
                "archive_rel_path": archive_partial.relative_to(
                    self.storage_layout.artifacts_root
                ).as_posix(),
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
                    self.storage_layout.artifacts_root
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
            for export in invalidated:
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
        snapshot_parent = self.storage_layout.staging_directory(
            f"snapshot-{uuid.uuid4().hex}"
        )
        snapshot = snapshot_parent / "source"
        try:
            self.store.mark_build_running(build_id, phase="snapshot")
            extract_git_archive(revision.bare_repo, revision.source_commit, snapshot, timeout=120)
            source_tree = load_problem_source_tree(
                snapshot,
                problem_limits=problem_config_limits(self.db.config_values),
                tests_spec_max_bytes=self.db.config_values.integer(
                    "TEXTAREA_MAX_BYTES"
                ),
                statement_sample_max_bytes=self.db.config_values.integer(
                    "STATEMENT_SAMPLE_MAX_BYTES"
                ),
            )
            self.store.mark_build_phase(build_id, "verification")
            completed_verification_id = verification_builder(
                snapshot, revision.source_commit, revision.revision_number, verification_id
            )
            if completed_verification_id != verification_id:
                raise RuntimeError("Native Package verification identity changed")
            return self._build_native_package(
                revision=revision,
                snapshot=snapshot,
                verification_id=verification_id,
                build_id=build_id,
                invalidate_exports=invalidate_exports,
                source_tree=source_tree,
            )
        except Exception as exc:
            self.store.mark_build_failed(build_id, str(exc))
            raise
        finally:
            shutil.rmtree(snapshot_parent, ignore_errors=True)

    @staticmethod
    def _payload_matches_descriptor(
        payload: PayloadFile | None,
        descriptor: NativePackageFileEntry | None,
    ) -> bool:
        if payload is None or descriptor is None:
            return payload is None and descriptor is None
        if payload.path.is_symlink() or not payload.path.is_file():
            return False
        return bool(
            int(payload.path.stat().st_size) == int(descriptor["size"])
            and sha256_file(payload.path) == str(descriptor["sha256"])
        )

    def _verification_evidence_mismatch(
        self,
        reader: NativePackageReader,
        verification_id: str,
    ) -> str:
        source_tree = load_problem_source_tree(
            reader.root,
            problem_limits=problem_config_limits(self.db.config_values),
            tests_spec_max_bytes=self.db.config_values.integer(
                "TEXTAREA_MAX_BYTES"
            ),
            statement_sample_max_bytes=self.db.config_values.integer(
                "STATEMENT_SAMPLE_MAX_BYTES"
            ),
            ignored_root_names=PACKAGE_DERIVED_ROOT_NAMES,
        )
        owners = self._verification_test_owners(verification_id, source_tree)
        for test in reader.manifest["tests"]:
            test_id = str(test["id"])
            owner = owners.by_source_id.get(test_id)
            if owner is None:
                return f"test {test_id} has no full Verification evidence"
            input_payload = self._artifact_file_resolver(owner.input_ref)
            if not self._payload_matches_descriptor(input_payload, test["input"]):
                return f"test {test_id} input differs from the existing Package"
            answer_payload = self._verification_payload(
                verification_id,
                owner.source_id,
                "answer_ref",
            )
            if not self._payload_matches_descriptor(
                answer_payload,
                test.get("answer"),
            ):
                return f"test {test_id} answer differs from the existing Package"
        return ""

    def _promote_materialization_verification(
        self,
        materialization: MaterializationRow,
        verification_id: str,
    ) -> str:
        certification = self.store.verification_certifications(
            [verification_id]
        ).get(verification_id)
        if certification is None:
            return "full Verification record is missing"
        certification_source = parse_verification_source(
            certification["source_commit"]
        )
        if (
            certification["problem_id"] != materialization["problem_id"]
            or certification_source.base_commit != materialization["source_commit"]
        ):
            return "full Verification does not identify this published revision"
        if (
            certification["kind"] != Kind.ALL.value
            or certification["status"] != VerificationStatus.OK.value
        ):
            return "Verification is not a successful full Verification"
        if materialization["verification_id"] == verification_id:
            return ""
        with self._validated_reader(materialization) as reader:
            mismatch = self._verification_evidence_mismatch(
                reader,
                verification_id,
            )
        if mismatch:
            return mismatch
        updated = self.store.update_materialization_verification(
            materialization["id"],
            expected_verification_id=materialization["verification_id"],
            verification_id=verification_id,
        )
        if not updated:
            return "Native Package certification changed concurrently"
        return ""

    def promote_native_package_verification(
        self,
        *,
        problem_id: int,
        source_commit: str,
        verification_id: str,
    ) -> str:
        with self._revision_operation(int(problem_id), source_commit):
            materialization = self.store.materialization_for_revision(
                int(problem_id),
                source_commit,
            )
            if (
                materialization is None
                or materialization["status"] != "available"
            ):
                return ""
            return self._promote_materialization_verification(
                materialization,
                verification_id,
            )

    def _verify_existing_native_package(
        self,
        revision: PublishedRevision,
        materialization: MaterializationRow,
        verification_builder: VerificationBuilder,
    ) -> MaterializationRow:
        verification_id = self._verification_id_allocator()
        snapshot_parent = self.storage_layout.staging_directory(
            f"snapshot-{uuid.uuid4().hex}"
        )
        snapshot = snapshot_parent / "source"
        try:
            extract_git_archive(
                revision.bare_repo,
                revision.source_commit,
                snapshot,
                timeout=120,
            )
            completed_verification_id = verification_builder(
                snapshot,
                revision.source_commit,
                revision.revision_number,
                verification_id,
            )
            if completed_verification_id != verification_id:
                raise RuntimeError("Native Package verification identity changed")
            mismatch = self._promote_materialization_verification(
                materialization,
                verification_id,
            )
            if mismatch:
                raise ValueError(
                    "Full Verification did not certify the existing Native Package: "
                    f"{mismatch}"
                )
            current = self.store.materialization(materialization["id"])
            if current is None:
                raise RuntimeError("Native Package disappeared after Verification")
            return current
        finally:
            shutil.rmtree(snapshot_parent, ignore_errors=True)

    def ensure_native_package(
        self,
        revision: PublishedRevision,
        verification_builder: VerificationBuilder,
        *,
        reuse_unverified: bool = False,
    ) -> MaterializationRow:
        problem_id = int(revision.problem["id"])
        with self._revision_operation(problem_id, revision.source_commit):
            existing = self.store.materialization_for_revision(
                problem_id,
                revision.source_commit,
            )
            if existing is not None:
                if existing["status"] == "available":
                    try:
                        self._validate_materialization(existing)
                    except Exception as exc:
                        self._invalidate_materialization(existing, str(exc))
                    else:
                        if (
                            reuse_unverified
                            or self.native_package_verified(existing)
                        ):
                            return existing
                        return self._verify_existing_native_package(
                            revision,
                            existing,
                            verification_builder,
                        )
            return self._run_materialization_build(
                revision,
                verification_builder,
                invalidate_exports=existing is not None,
            )

    def rebuild_native_package(
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
                    raise ValueError("Polygon Replica package contains an unsafe path")
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise ValueError("Polygon Replica package contains a symbolic link")
                file_type = stat.S_IFMT(mode)
                if file_type and not (stat.S_ISDIR(mode) if info.is_dir() else stat.S_ISREG(mode)):
                    raise ValueError("Polygon Replica package contains a special file")
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
            raise FrozenNativePackageMismatch(
                "frozen Native Package checksum no longer matches"
            )
        archive_path = self._archive_path(row)
        if archive_path.is_symlink() or not archive_path.is_file():
            raise ValueError("Native Package archive is missing")
        if int(archive_path.stat().st_size) != row["archive_size_bytes"]:
            raise ValueError("Native Package archive size changed")
        if sha256_file(archive_path) != row["archive_sha256"]:
            raise ValueError("Native Package archive checksum changed")
        extraction = self.storage_layout.staging_directory(
            f"read-{uuid.uuid4().hex}"
        )
        extraction.mkdir(parents=True, exist_ok=False)
        try:
            self._safe_extract(archive_path, extraction)
            manifest_path = extraction / TEST_DATA_DIR / "manifest.json"
            manifest = load_manifest(manifest_path)
            if manifest["source_commit"] != row["source_commit"]:
                raise ValueError("Native Package manifest commit does not match metadata")
            if manifest["revision_number"] != row["revision_number"]:
                raise ValueError("Native Package manifest number does not match metadata")
            if manifest["source_digest"] != row["source_digest"]:
                raise ValueError("Native Package source digest does not match metadata")
            validate_manifest_files(
                extraction,
                manifest,
                tests_spec_max_bytes=self.db.config_values.integer(
                    "TEXTAREA_MAX_BYTES"
                ),
                statement_sample_max_bytes=self.db.config_values.integer(
                    "STATEMENT_SAMPLE_MAX_BYTES"
                ),
            )
            source_tree = load_problem_source_tree(
                extraction,
                problem_limits=problem_config_limits(self.db.config_values),
                tests_spec_max_bytes=self.db.config_values.integer(
                    "TEXTAREA_MAX_BYTES"
                ),
                statement_sample_max_bytes=self.db.config_values.integer(
                    "STATEMENT_SAMPLE_MAX_BYTES"
                ),
                ignored_root_names=PACKAGE_DERIVED_ROOT_NAMES,
            )
            if source_tree.problem["mode"] != manifest["mode"]:
                raise ValueError(
                    "Native Package mode does not match config/problem.json"
                )
            if source_tree.problem["pass_limit"] != manifest["pass_limit"]:
                raise ValueError(
                    "Native Package pass limit does not match config/problem.json"
                )
            yield NativePackageReader(
                native_package=row,
                root=extraction,
                manifest=manifest,
            )
            if check_current:
                if archive_path.is_symlink() or not archive_path.is_file():
                    raise FrozenNativePackageMismatch(
                        "Native Package archive changed while it was being read"
                    )
                if (
                    int(archive_path.stat().st_size) != row["archive_size_bytes"]
                    or sha256_file(archive_path) != row["archive_sha256"]
                ):
                    raise FrozenNativePackageMismatch(
                        "Native Package archive changed while it was being read"
                    )
                current = self.store.materialization(row["id"])
                if (
                    current is None
                    or current["status"] != "available"
                    or current["archive_sha256"] != row["archive_sha256"]
                ):
                    raise FrozenNativePackageMismatch(
                        "Native Package changed while it was being read"
                    )
                if (
                    expected_archive_sha256 is not None
                    and current["archive_sha256"] != expected_archive_sha256
                ):
                    raise FrozenNativePackageMismatch(
                        "frozen Native Package checksum no longer matches"
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
        native_package_id: str,
        *,
        expected_archive_sha256: str | None = None,
    ) -> Iterator[NativePackageReader]:
        with self.native_package_operation(native_package_id) as row:
            if row["status"] != "available":
                raise ValueError("Native Package is unavailable")
            validation = self._validated_reader(
                row,
                expected_archive_sha256=expected_archive_sha256,
            )
            try:
                reader = validation.__enter__()
            except FrozenNativePackageMismatch:
                raise
            except Exception as exc:
                cleanup_error = self._invalidate_materialization(row, str(exc))
                detail = f"; cleanup failed: {cleanup_error}" if cleanup_error else ""
                raise ValueError(
                    f"Native Package integrity check failed: {exc}{detail}"
                ) from exc
            try:
                yield reader
            except BaseException as exc:
                validation.__exit__(type(exc), exc, exc.__traceback__)
                raise
            else:
                validation.__exit__(None, None, None)

    def native_package_archive(
        self,
        native_package_id: str,
        *,
        expected_archive_sha256: str | None = None,
    ) -> tuple[NativePackage, Path]:
        with self.native_package_operation(native_package_id) as row:
            if row["status"] != "available":
                raise ValueError("Native Package is unavailable")
            try:
                self._validate_materialization(
                    row,
                    expected_archive_sha256=expected_archive_sha256,
                )
            except FrozenNativePackageMismatch:
                raise
            except Exception as exc:
                cleanup_error = self._invalidate_materialization(row, str(exc))
                detail = f"; cleanup failed: {cleanup_error}" if cleanup_error else ""
                raise ValueError(
                    f"Native Package integrity check failed: {exc}{detail}"
                ) from exc
            return row, self._archive_path(row)

    def open_native_package_download(
        self,
        native_package_id: str,
    ) -> NativePackageDownload:
        operation = self.native_package_operation(native_package_id)
        row = operation.__enter__()
        try:
            if row["status"] != "available":
                raise ValueError("Native Package is unavailable")
            try:
                self._validate_materialization(row)
            except FrozenNativePackageMismatch:
                raise
            except Exception as exc:
                cleanup_error = self._invalidate_materialization(row, str(exc))
                detail = f"; cleanup failed: {cleanup_error}" if cleanup_error else ""
                raise ValueError(
                    f"Native Package integrity check failed: {exc}{detail}"
                ) from exc
            stream = self._archive_path(row).open("rb")
        except BaseException as exc:
            operation.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return NativePackageDownload(
            native_package=row,
            stream=stream,
            operation=operation,
        )

    def recover_startup(self) -> int:
        staging = self.storage_layout.artifact_staging_root
        if staging.exists() and staging.is_dir() and not staging.is_symlink():
            for child in staging.iterdir():
                if child.is_symlink():
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file():
                    child.unlink(missing_ok=True)
        return self.store.fail_interrupted_builds()
