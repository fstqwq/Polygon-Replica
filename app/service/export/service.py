from __future__ import annotations
# pylint: disable=too-many-lines

import hashlib
import os
import re
import shutil
import threading
import uuid
import zipfile
from pathlib import Path

from app.config import ConfigValues
from app.db import DB
from app.service.disk.export_store import ExportJobRow, ExportStore
from app.service.export.icpc_package import (
    SUBMISSION_RULES,
    annotated_submission,
    render_problem_yaml,
    render_submissions_yaml,
    source_language,
    statement_language_code,
    write_input_validator,
    write_output_validator,
)
from app.service.platform.hashing import sha256_file
from app.service.platform.fs.op import extract_git_archive, remove_symlinks
from app.service.platform.fs.layout import StorageLayout
from app.service.problem.build_config import BuildConfig, load_build_config
from app.service.problem.runtime_config import (
    ProblemConfig,
    load_problem_config,
    problem_config_limits,
)
from app.service.problem.solution_metadata import load_solution_desc
from app.service.problem.source_file import resolve_source
from app.service.statement.render import render_statement_main
from app.service.statement.tex_compile import TexCompileService
from app.service.statement.context import statement_languages
from app.service.statement.title import statement_title_from_snapshot
from app.service.platform.workspace_path import (
    is_allowed_workspace_root_path,
    is_hidden_workspace_path,
    is_repository_answer_path,
)
from app.service.problem_package.service import NativePackageReader, ProblemPackageService
from app.service.problem_package.statement_samples import hydrate_native_statement_samples


class ExportService:
    TYPES = {
        "icpc": "icpc.zip",
        "native": "native.zip",
    }
    SOURCE_SUFFIX_ORDER = (".cpp", ".cc", ".cxx", ".c++", ".py", ".java")
    DOMJUDGE_COLOR_PALETTE = (
        "#e6194b",
        "#3cb44b",
        "#ffe119",
        "#4363d8",
        "#f58231",
        "#911eb4",
        "#46f0f0",
        "#f032e6",
        "#bcf60c",
        "#fabebe",
        "#008080",
        "#e6beff",
        "#9a6324",
        "#fffac8",
        "#800000",
        "#aaffc3",
        "#808000",
        "#ffd8b1",
    )

    def __init__(
        self,
        db: DB,
        storage_layout: StorageLayout,
        tex_compile_service: TexCompileService,
        problem_package_service: ProblemPackageService,
        config_values: ConfigValues,
    ):
        self.db = db
        self._store = ExportStore(db)
        self.storage_layout = storage_layout
        self.tex_compile_service = tex_compile_service
        self.problem_package_service = problem_package_service
        self._config_values = config_values
        self._conversion_locks_guard = threading.Lock()
        self._conversion_locks: dict[tuple[str, str], threading.Lock] = {}

    def _conversion_lock(
        self,
        materialization_id: str,
        export_type: str,
    ) -> threading.Lock:
        key = (materialization_id, export_type)
        with self._conversion_locks_guard:
            return self._conversion_locks.setdefault(key, threading.Lock())

    def latest_source_commit(self, problem_id: int) -> str:
        return self._store.latest_source_commit(problem_id)

    def export_archive_path(self, problem_id: int, export_id: str, filename: str) -> Path | None:
        row = self._store.export_archive_row(int(problem_id), export_id)
        if row is None:
            return None
        stored_filename = Path(str(row["filename"] or "").strip()).name
        archive_name = Path(str(filename or "").strip()).name
        if not stored_filename or stored_filename != archive_name:
            return None
        if str(row["export_type"]) == "native":
            try:
                _materialization, candidate = self.problem_package_service.native_archive(
                    str(row["materialization_id"])
                )
            except ValueError:
                return None
            return candidate
        try:
            root = self.storage_layout.artifacts_root.resolve()
            candidate = self.storage_layout.resolve_artifact(
                str(row["archive_rel_path"])
            )
            if root not in candidate.parents:
                return None
        except (ValueError, OSError):
            return None
        if (
            not candidate.exists()
            or not candidate.is_file()
            or candidate.is_symlink()
            or candidate.stat().st_size != row["size_bytes"]
            or sha256_file(candidate) != row["sha256"]
        ):
            return None
        return candidate

    def problem_export_jobs(
        self,
        problem_id: int,
        *,
        limit: int,
    ) -> list[ExportJobRow]:
        return self._store.problem_export_jobs(
            int(problem_id),
            limit=limit,
        )

    def latest_succeeded_export_job(
        self,
        problem_id: int,
        source_commit: str,
        export_type: str,
    ) -> ExportJobRow | None:
        return self._store.latest_succeeded_export_job(
            int(problem_id),
            source_commit,
            export_type,
        )

    def export_job(
        self,
        problem_id: int,
        actor_user_id: int,
        job_id: str,
        *,
        include_all: bool = False,
    ) -> ExportJobRow | None:
        return self._store.export_job(
            int(problem_id),
            int(actor_user_id),
            job_id,
            include_all=include_all,
        )

    def create_export_job(
        self,
        *,
        job_id: str,
        problem_id: int,
        actor_user_id: int,
        export_type: str,
        source_commit: str,
    ) -> None:
        self._store.create_export_job(
            job_id=job_id,
            problem_id=int(problem_id),
            actor_user_id=int(actor_user_id),
            export_type=export_type,
            source_commit=source_commit,
        )

    def mark_export_job_running(
        self,
        job_id: str,
        *,
        source_commit: str,
    ) -> None:
        self._store.mark_export_job_running(
            job_id,
            source_commit=source_commit,
        )

    def mark_export_job_succeeded(
        self,
        job_id: str,
        *,
        materialization_id: str,
        export_id: str,
    ) -> None:
        self._store.mark_export_job_succeeded(
            job_id,
            materialization_id=materialization_id,
            export_id=export_id,
        )

    def mark_export_job_failed(self, job_id: str, error: str) -> None:
        self._store.mark_export_job_failed(job_id, error)

    def fail_interrupted_export_jobs(self) -> int:
        return self._store.fail_interrupted_export_jobs()

    def _package_root_name(self, slug: str) -> str:
        return "".join(ch.lower() for ch in slug if ch.isalnum()) or "problem"

    def _archive_filename_slug(self, slug: str) -> str:
        token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(slug or "").strip())
        token = token.strip("-.")
        return token or "problem"

    def _public_problem_slug(self, slug: str) -> str:
        token = str(slug or "").replace("\\", "/").strip("/").rsplit("/", 1)[-1].strip()
        return self._archive_filename_slug(token)

    def _domjudge_color(self, slug: str) -> str:
        digest = hashlib.sha256(str(slug or "problem").encode("utf-8")).digest()
        return self.DOMJUDGE_COLOR_PALETTE[digest[0] % len(self.DOMJUDGE_COLOR_PALETTE)]

    def _is_safe_regular_file(self, root: Path, p: Path, root_resolved: Path | None = None) -> bool:
        if p.is_symlink() or not p.exists() or not p.is_file():
            return False
        return self._is_safe_path_within(root, p, root_resolved=root_resolved)

    def _is_safe_path_within(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        try:
            resolved_root = root_resolved if root_resolved is not None else root.resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents or resolved_root == resolved

    def _iter_safe_descendant_files(self, root: Path):
        if not root.exists() or not root.is_dir():
            return
        if root.is_symlink():
            return
        root_resolved = root.resolve()
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if root_resolved not in dir_root_resolved.parents and root_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            keep_dirs: list[str] = []
            for name in dirnames:
                d = dir_root / name
                if d.is_symlink():
                    continue
                keep_dirs.append(name)
            dirnames[:] = sorted(keep_dirs)

            safe_filenames: list[str] = []
            for name in filenames:
                p = dir_root / name
                if p.is_symlink():
                    continue
                if not p.is_file():
                    continue
                safe_filenames.append(name)

            for name in sorted(safe_filenames):
                yield dir_root / name

    def _copy_dir_contents(self, src: Path, dst: Path) -> None:
        if not src.exists() or not src.is_dir():
            return
        for p in self._iter_safe_descendant_files(src):
            rel = p.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)

    def _iter_solution_sources(self, folder: Path):
        if not folder.exists() or not folder.is_dir():
            return
        try:
            _ = folder.resolve()
        except OSError:
            return
        suffix_rank = {suffix: idx for idx, suffix in enumerate(self.SOURCE_SUFFIX_ORDER)}
        matched: list[tuple[int, str]] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = str(entry.name or "")
                    suffix = Path(name).suffix.lower()
                    rank = suffix_rank.get(suffix)
                    if rank is None:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    matched.append((rank, name))
        except OSError:
            return
        for _rank, name in sorted(matched, key=lambda item: (item[0], item[1])):
            yield folder / name

    def _solution_expected_behavior(self, source_file: Path) -> str:
        root = source_file.parent.parent
        source_rel = source_file.relative_to(root).as_posix()
        return load_solution_desc(root, source_rel)["expected_behavior"]

    def _ensure_unique_file_path(self, parent: Path, filename: str) -> Path:
        safe_name = Path(str(filename or "")).name
        if not safe_name:
            safe_name = "solution.cpp"
        target = parent / safe_name
        if not target.exists():
            return target
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        idx = 2
        while True:
            candidate = parent / f"{stem}-{idx}{suffix}"
            if not candidate.exists():
                return candidate
            idx += 1

    def _load_build_config(self, snapshot: Path) -> BuildConfig:
        return load_build_config(snapshot)

    def _resolve_snapshot_source(self, snapshot: Path, rel_path: str) -> Path:
        try:
            return resolve_source(snapshot, rel_path)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    def _effective_checker_source(
        self,
        snapshot: Path,
        build_cfg: BuildConfig,
    ) -> Path | None:
        checker_source = build_cfg.get("checker_source")
        if checker_source is None:
            return None
        return self._resolve_snapshot_source(snapshot, checker_source)

    def _effective_validator_source(
        self,
        snapshot: Path,
        build_cfg: BuildConfig,
    ) -> Path | None:
        configured = build_cfg.get("validator_source")
        if configured is None:
            return None
        return self._resolve_snapshot_source(snapshot, configured)

    def _effective_interactor_source(
        self,
        snapshot: Path,
        build_cfg: BuildConfig,
    ) -> Path | None:
        configured = build_cfg.get("interactor_source")
        if configured is None:
            return None
        return self._resolve_snapshot_source(snapshot, configured)

    def _copy_native_working_tree(self, src_dir: Path, dst_dir: Path, *, root_dir: Path) -> None:
        for child in src_dir.iterdir():
            rel = child.relative_to(root_dir)
            if rel.parts and rel.parts[0] in {"temp", "draft"}:
                continue
            if is_hidden_workspace_path(rel.parts):
                continue
            if not is_allowed_workspace_root_path(rel.parts):
                continue
            if is_repository_answer_path(rel.parts):
                continue
            if child.is_symlink():
                continue
            target = dst_dir / child.name
            if child.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                self._copy_native_working_tree(child, target, root_dir=root_dir)
                continue
            shutil.copy2(child, target)

    def _snapshot_working_tree(
        self,
        workspace_id: int | None,
        problem_slug: str,
        tmp_root: Path,
        *,
        source_commit: str | None = None,
    ) -> Path:
        """Copy a working tree or committed revision using native-import path rules."""
        workspace = self._workspace_path_for_snapshot(workspace_id, problem_slug)
        snapshot = tmp_root / "_source"
        if source_commit is None:
            snapshot.mkdir(parents=True, exist_ok=True)
            self._copy_native_working_tree(workspace, snapshot, root_dir=workspace)
        else:
            extract_git_archive(workspace, source_commit, snapshot, timeout=120)
        remove_symlinks(snapshot)
        return snapshot

    def _workspace_path_for_snapshot(self, workspace_id: int | None, problem_slug: str) -> Path:
        if workspace_id is None:
            raise ValueError("build workspace metadata missing")
        ws_row = self._store.workspace_export_context(int(workspace_id))
        if ws_row is None:
            raise ValueError(f"workspace metadata not found: {workspace_id}")
        workspace = Path(ws_row["path"]).resolve()
        expected_workspace = self.storage_layout.workspace(
            str(ws_row["username"]),
            problem_slug,
        )
        if workspace != expected_workspace:
            raise ValueError(f"workspace path mismatch for export workspace {workspace_id}")
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError(f"workspace path missing for export workspace {workspace_id}")
        git_dir = workspace / ".git"
        if not git_dir.exists() or not git_dir.is_dir():
            raise ValueError(f"workspace git metadata missing for export workspace {workspace_id}")
        return workspace

    def _load_problem_config(self, snapshot: Path) -> ProblemConfig:
        return load_problem_config(
            snapshot,
            limits=problem_config_limits(self._config_values),
        )

    def _build_problem_yaml(
        self,
        *,
        problem_slug: str,
        source_commit: str,
        statement_names: dict[str, str],
        mode: str,
        pass_limit: int,
        snapshot: Path,
    ) -> str:
        cfg = self._load_problem_config(snapshot)
        time_limit_ms = cfg["time_limit_ms"]
        memory_limit_mb = cfg["memory_limit_mb"]
        return render_problem_yaml(
            problem_slug=problem_slug,
            source_commit=source_commit,
            names=statement_names,
            mode=mode,
            pass_limit=pass_limit,
            time_limit_ms=time_limit_ms,
            memory_limit_mb=memory_limit_mb,
        )

    @staticmethod
    def _ini_value(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9 .,_/-]+", value):
            return value
        escaped = value.replace("\\", "\\\\").replace('"', r'\"')
        return f'"{escaped}"'

    @staticmethod
    def _domjudge_short_name(value: str) -> str:
        short_name = value.strip()
        if not short_name:
            raise ValueError("DOMjudge short-name is required")
        if "\n" in short_name or "\r" in short_name:
            raise ValueError("DOMjudge short-name must be a single line")
        return short_name

    def _build_domjudge_problem_ini(
        self,
        *,
        problem_name: str,
        external_id: str,
        short_name: str,
        snapshot: Path,
    ) -> str:
        cfg = self._load_problem_config(snapshot)
        seconds = float(cfg["time_limit_ms"]) / 1000.0
        return (
            f"name = {self._ini_value(problem_name)}\n"
            f"externalid = {external_id}\n"
            f"short-name = {short_name}\n"
            f"timelimit = {seconds:.3f}".rstrip("0").rstrip(".") + "\n"
            f"color = {self._domjudge_color(external_id)}\n"
        )

    def _statement_export_languages(self, snapshot: Path) -> list[str]:
        languages = statement_languages(snapshot)
        if not languages:
            raise ValueError("ICPC export requires at least one problem statement")
        return languages

    @staticmethod
    def _statement_export_suffix(language: str) -> str:
        return statement_language_code(language)

    def _try_compile_statement_pdf(
        self,
        snapshot: Path,
        dst_statement: Path,
        *,
        problem_name: str,
        include_sample_tests: bool = True,
    ) -> bool:
        compiled_any = False
        limits = self.db.config_values.snapshot()
        tests_spec_max_bytes = int(limits["TEXTAREA_MAX_BYTES"])
        statement_sample_max_bytes = int(
            limits["STATEMENT_SAMPLE_MAX_BYTES"]
        )
        for language in self._statement_export_languages(snapshot):
            try:
                rendered = render_statement_main(
                    snapshot / "statement",
                    problem_title=problem_name,
                    language=language,
                    include_sample_tests=include_sample_tests,
                    tests_spec_max_bytes=tests_spec_max_bytes,
                    statement_sample_max_bytes=statement_sample_max_bytes,
                    problem_limits=problem_config_limits(self._config_values),
                )
            except Exception as exc:
                raise ValueError(f"failed to render {language} statement: {exc}") from exc
            compile_result = self.tex_compile_service.compile_pdf(rendered)
            proc = compile_result.proc
            if proc.returncode != 0:
                error = str(proc.stderr or proc.stdout or "statement compiler failed").strip()
                raise ValueError(f"failed to compile {language} statement: {error}")
            pdf_path = compile_result.pdf_path
            if not pdf_path.exists() or not pdf_path.is_file():
                raise ValueError(f"failed to compile {language} statement: PDF was not produced")
            suffix = self._statement_export_suffix(language)
            dst_statement.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf_path, dst_statement / f"problem.{suffix}.pdf")
            compiled_any = True
        return compiled_any

    @staticmethod
    def _keep_samples_out_of_domjudge_sample_data(mode: str, pass_limit: int) -> bool:
        return mode == "interactive" or pass_limit > 1

    def _copy_secret_and_sample_data(
        self,
        native: NativePackageReader,
        package_root: Path,
        *,
        samples_as_secret: bool = False,
    ) -> None:
        secret_dir = package_root / "data" / "secret"
        sample_dir = package_root / "data" / "sample"
        secret_dir.mkdir(parents=True, exist_ok=True)
        sample_dir.mkdir(parents=True, exist_ok=True)
        for row in native.manifest["tests"]:
            test_id = row["id"]
            destination = secret_dir if samples_as_secret or not row["sample"] else sample_dir
            input_source = native.payload(row, "input")
            if destination == sample_dir:
                input_source = native.payload(row, "sample_input") or input_source
            if input_source is None:
                raise ValueError(f"Native test input is missing: {test_id}")
            shutil.copy2(input_source, destination / f"{test_id}.in")
            answer_source = native.payload(row, "answer")
            if destination == sample_dir:
                answer_source = native.payload(row, "sample_output") or answer_source
            answer_target = destination / f"{test_id}.ans"
            if answer_source is None:
                if native.manifest["mode"] != "interactive":
                    raise ValueError(f"Native test answer is missing: {test_id}")
                answer_target.write_bytes(b"")
            else:
                shutil.copy2(answer_source, answer_target)

    def _copy_solutions(
        self,
        snapshot: Path,
        package_root: Path,
    ) -> None:
        dst_submissions = package_root / "submissions"
        dst_submissions.mkdir(parents=True, exist_ok=True)
        metadata: dict[str, dict[str, object]] = {}
        accepted_count = 0
        for source_file in self._iter_solution_sources(snapshot / "solutions"):
            expected = self._solution_expected_behavior(source_file)
            rule = SUBMISSION_RULES.get(expected)
            if rule is None:
                continue
            target_dir = dst_submissions / rule["directory"]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = self._ensure_unique_file_path(target_dir, source_file.name)
            if expected in {"tle_or_correct", "tle_or_re", "rejected"}:
                target.write_bytes(annotated_submission(source_file, rule["domjudge_results"]))
            else:
                shutil.copy2(source_file, target)
            rel = target.relative_to(dst_submissions).as_posix()
            metadata[rel] = {
                "language": source_language(source_file),
                "permitted": list(rule["permitted"]),
                "required": list(rule["required"]),
            }
            if expected == "accepted":
                accepted_count += 1
        if accepted_count == 0:
            raise ValueError("2025-09 export requires at least one accepted submission")
        (dst_submissions / "submissions.yaml").write_text(
            render_submissions_yaml(metadata),
            encoding="utf-8",
            newline="\n",
        )

    def _build_icpc_package(
        self,
        *,
        package_root: Path,
        native: NativePackageReader,
        problem_name: str,
        problem_slug: str,
        source_commit: str,
        mode: str,
        pass_limit: int,
    ) -> None:
        snapshot = native.root
        package_root.mkdir(parents=True, exist_ok=True)
        build_cfg = self._load_build_config(snapshot)
        checker_source = (
            None
            if mode == "interactive"
            else self._effective_checker_source(snapshot, build_cfg)
        )
        interactor_source = (
            self._effective_interactor_source(snapshot, build_cfg)
            if mode == "interactive"
            else None
        )
        if mode == "interactive" and interactor_source is None:
            raise ValueError("interactive export requires interactor source")
        validator_source = self._effective_validator_source(snapshot, build_cfg)

        samples_as_secret = self._keep_samples_out_of_domjudge_sample_data(mode, pass_limit)
        limits = self.db.config_values.snapshot()
        hydrate_native_statement_samples(
            native,
            tests_spec_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            statement_sample_max_bytes=int(
                limits["STATEMENT_SAMPLE_MAX_BYTES"]
            ),
        )

        statement_languages_to_export = self._statement_export_languages(snapshot)
        statement_dir = package_root / "statement"
        self._try_compile_statement_pdf(
            snapshot,
            statement_dir,
            problem_name=problem_name,
            include_sample_tests=not samples_as_secret,
        )
        statement_names: dict[str, str] = {}
        statement_files: dict[str, Path] = {}
        for language in statement_languages_to_export:
            language_code = self._statement_export_suffix(language)
            if language_code in statement_files:
                raise ValueError(f"duplicate statement language code: {language_code}")
            statement_file = statement_dir / f"problem.{language_code}.pdf"
            if not statement_file.is_file():
                raise ValueError(f"failed to compile {language} statement: PDF was not produced")
            statement_names[language_code] = statement_title_from_snapshot(
                snapshot,
                fallback_title=problem_name,
                language=language,
            )
            statement_files[language_code] = statement_file
        legacy_statement_dir = package_root / "problem_statement"
        legacy_statement_dir.mkdir(parents=True, exist_ok=True)
        for language_code, statement_file in statement_files.items():
            shutil.copy2(statement_file, legacy_statement_dir / statement_file.name)
        preferred_code = "en" if "en" in statement_files else next(iter(statement_files))
        shutil.copy2(statement_files[preferred_code], legacy_statement_dir / "problem.pdf")

        (package_root / "problem.yaml").write_text(
            self._build_problem_yaml(
                problem_slug=problem_slug,
                source_commit=source_commit,
                statement_names=statement_names,
                mode=mode,
                pass_limit=pass_limit,
                snapshot=snapshot,
            ),
            encoding="utf-8",
            newline="\n",
        )
        (package_root / "domjudge-problem.ini").write_text(
            self._build_domjudge_problem_ini(
                problem_name=problem_name,
                external_id=self._public_problem_slug(problem_slug),
                short_name=self._domjudge_short_name(
                    self._public_problem_slug(problem_slug)
                ),
                snapshot=snapshot,
            ),
            encoding="utf-8",
        )
        self._copy_secret_and_sample_data(
            native,
            package_root,
            samples_as_secret=samples_as_secret,
        )
        write_input_validator(
            snapshot=snapshot,
            package_root=package_root,
            source=validator_source,
        )
        write_output_validator(
            snapshot=snapshot,
            package_root=package_root,
            source=interactor_source if mode == "interactive" else checker_source,
        )
        self._copy_solutions(snapshot, package_root)
        self._copy_attachments(snapshot, package_root)

    def _make_archive_from_dir_contents(self, archive_target: Path, root: Path) -> Path:
        archive_target.parent.mkdir(parents=True, exist_ok=True)
        root_resolved = root.resolve()
        with zipfile.ZipFile(archive_target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
                dir_root = Path(dirpath)
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if not (dir_root / name).is_symlink()
                )
                try:
                    dir_root_resolved = dir_root.resolve()
                except OSError:
                    dirnames[:] = []
                    continue
                if root_resolved not in dir_root_resolved.parents and root_resolved != dir_root_resolved:
                    dirnames[:] = []
                    continue
                rel_dir = dir_root.relative_to(root)
                if rel_dir.parts:
                    zf.writestr(rel_dir.as_posix().rstrip("/") + "/", b"")
                for filename in sorted(filenames):
                    source = dir_root / filename
                    if not self._is_safe_regular_file(root, source, root_resolved=root_resolved):
                        continue
                    zf.write(source, source.relative_to(root).as_posix())
        return archive_target

    def _copy_attachments(self, snapshot: Path, package_root: Path) -> None:
        src = snapshot / "attachments"
        if not src.exists() or not src.is_dir() or src.is_symlink():
            return
        dst = package_root / "attachments"
        dst.mkdir(parents=True, exist_ok=True)
        self._copy_dir_contents(src, dst)

    def _build_native_package(
        self,
        *,
        package_root: Path,
        snapshot: Path,
    ) -> None:
        package_root.mkdir(parents=True, exist_ok=True)
        self._copy_native_working_tree(snapshot, package_root, root_dir=snapshot)

    def create_workspace_snapshot(
        self,
        problem: str,
        *,
        workspace_id: int,
        source_commit: str | None = None,
        revision_number: int | None = None,
    ) -> Path:
        if (source_commit is None) != (revision_number is None):
            raise ValueError("snapshot revision identity is incomplete")
        if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise ValueError("snapshot source commit is invalid")
        if revision_number is not None and revision_number < 1:
            raise ValueError("snapshot revision number is invalid")
        problem_row = self._store.problem_export_row(problem)
        if problem_row is None:
            raise ValueError(f"unknown problem: {problem}")
        snapshots_root = self.storage_layout.export_snapshot_root
        snapshots_root.mkdir(parents=True, exist_ok=True)
        tmp_parent = self.storage_layout.export_snapshot(
            f"snap-{uuid.uuid4().hex[:12]}"
        )
        tmp_root = tmp_parent / "work"
        package_root = tmp_root / self._package_root_name(str(problem_row["slug"]))
        try:
            package_root.mkdir(parents=True, exist_ok=True)
            snapshot = self._snapshot_working_tree(
                int(workspace_id),
                str(problem_row["slug"]),
                tmp_root,
                source_commit=source_commit,
            )
            self._build_native_package(
                package_root=package_root,
                snapshot=snapshot,
            )
            revision_suffix = f"-v{revision_number}" if revision_number is not None else ""
            archive_stem = tmp_parent / (
                f"{self._archive_filename_slug(str(problem_row['slug']))}"
                f"{revision_suffix}-snapshot"
            )
            archive = shutil.make_archive(
                str(archive_stem),
                "zip",
                root_dir=tmp_root,
                base_dir=package_root.name,
            )
            return Path(archive)
        except Exception:
            shutil.rmtree(tmp_parent, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def _export_problem_root(self, problem_slug: str) -> Path:
        return self.storage_layout.export_problem(
            self._archive_filename_slug(problem_slug)
        )

    def _export_dir(self, problem_slug: str, export_id: str) -> Path:
        return self.storage_layout.export_directory(
            self._archive_filename_slug(problem_slug),
            export_id,
        )

    def _export_path(self, problem_slug: str, export_id: str, filename: str) -> Path:
        return self.storage_layout.export_archive(
            self._archive_filename_slug(problem_slug),
            export_id,
            filename,
        )

    def _cached_export_path(
        self,
        *,
        problem_id: int,
        materialization_id: str,
        export_type: str,
    ) -> tuple[str, Path] | None:
        export_id = self._store.cached_export(
            materialization_id=materialization_id,
            export_type=export_type,
        )
        if not export_id:
            return None
        row = self._store.export_archive_row(problem_id, export_id)
        if row is not None:
            if export_type == "native":
                try:
                    _materialization, path = self.problem_package_service.native_archive(materialization_id)
                    return export_id, path
                except ValueError:
                    pass
            else:
                path = self.storage_layout.resolve_artifact(row["archive_rel_path"])
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_size == row["size_bytes"]
                    and sha256_file(path) == row["sha256"]
                ):
                    return export_id, path
        self._store.delete_export(export_id)
        return None

    def create_export(
        self,
        problem: str,
        export_type: str,
        *,
        materialization_id: str,
        expected_archive_sha256: str | None = None,
    ) -> tuple[str, Path]:
        safe_export_type = str(export_type).strip().lower()
        lock = self._conversion_lock(materialization_id, safe_export_type)
        with self.problem_package_service.materialization_operation(materialization_id):
            with lock:
                return self._create_export(
                    problem,
                    safe_export_type,
                    materialization_id=materialization_id,
                    expected_archive_sha256=expected_archive_sha256,
                )

    def _create_export(
        self,
        problem: str,
        export_type: str,
        *,
        materialization_id: str,
        expected_archive_sha256: str | None = None,
    ) -> tuple[str, Path]:
        """Return a cached or newly converted artifact from one validated Native."""

        resolved_export_type = str(export_type or "").strip().lower() or "icpc"
        if resolved_export_type not in self.TYPES:
            raise ValueError("unsupported export type")
        problem_row = self._store.problem_export_row(problem)
        if problem_row is None:
            raise ValueError(f"unknown problem: {problem}")
        materialization = self.problem_package_service.store.materialization(materialization_id)
        if materialization is None or materialization["problem_id"] != int(problem_row["id"]):
            raise ValueError("Package does not belong to the problem")
        materialization, _native_path = self.problem_package_service.native_archive(
            materialization_id,
            expected_archive_sha256=expected_archive_sha256,
        )
        public_slug = self._public_problem_slug(str(problem_row["slug"]))
        cached = self._cached_export_path(
            problem_id=int(problem_row["id"]),
            materialization_id=materialization_id,
            export_type=resolved_export_type,
        )
        if cached is not None:
            return cached
        export_id = f"e-{uuid.uuid4().hex[:10]}"
        revision_token = f"v{materialization['revision_number']}"
        if resolved_export_type == "native":
            materialization, out = self.problem_package_service.native_archive(
                materialization_id,
                expected_archive_sha256=expected_archive_sha256,
            )
            filename = f"{self._archive_filename_slug(str(problem_row['slug']))}-native-{revision_token}.zip"
        else:
            filename = f"{public_slug}-{revision_token}.zip"
            staging = self.storage_layout.staging_directory(
                f"export-{export_id}-{uuid.uuid4().hex}"
            )
            package_root = staging / "package"
            archive_partial = staging / f"{filename}.partial"
            try:
                with self.problem_package_service.open_reader(
                    materialization_id,
                    expected_archive_sha256=expected_archive_sha256,
                ) as native:
                    mode = native.manifest["mode"]
                    pass_limit = native.manifest["pass_limit"]
                    problem_name = statement_title_from_snapshot(
                        native.root,
                        fallback_title=public_slug,
                    )
                    self._build_icpc_package(
                        package_root=package_root,
                        native=native,
                        problem_name=problem_name,
                        problem_slug=str(problem_row["slug"]),
                        source_commit=materialization["source_commit"],
                        mode=mode,
                        pass_limit=pass_limit,
                    )
                    self._make_archive_from_dir_contents(archive_partial, package_root)
                out = self._export_path(str(problem_row["slug"]), export_id, filename)
                out.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archive_partial, out)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        self._store.insert_export_record(
            export_id=export_id,
            problem_id=int(problem_row["id"]),
            materialization_id=materialization_id,
            export_type=resolved_export_type,
            filename=filename,
            archive_rel_path=out.relative_to(
                self.storage_layout.artifacts_root
            ).as_posix(),
            sha256=sha256_file(out),
            size_bytes=int(out.stat().st_size),
            source_commit=materialization["source_commit"],
        )
        return export_id, out
