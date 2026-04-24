from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Callable

from app.db import DB
from app.service.disk.export_store import ExportStore
from app.service.platform.hashing import sha256_file
from app.service.platform.fs.op import extract_git_archive, remove_symlinks
from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec, payload_rel_path_for_test
from app.service.problem.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior, parse_solution_desc
from app.service.statement.render import render_statement_main
from app.service.statement.tex_compile import TexCompileService
from app.service.statement.context import pick_statement_language, statement_languages
from app.service.platform.git_process import run_git
from app.service.platform.workspace_path import (
    is_allowed_workspace_root_path,
    is_hidden_workspace_path,
    is_repository_answer_path,
)


class ExportService:
    TYPES = {
        "icpc": "icpc.zip",
        "native": "native.zip",
    }
    SOURCE_SUFFIX_ORDER = (".cpp", ".cc", ".cxx", ".c", ".py", ".java")
    KATTIS_SUBMISSION_DIRS = (
        "accepted",
        "wrong_answer",
        "time_limit_exceeded",
        "run_time_error",
        "rejected",
    )
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
        artifacts_root: Path,
        workspace_root: Path,
        tex_compile_service: TexCompileService,
        artifact_blob_resolver: Callable[[str], bytes | None],
    ):
        self.db = db
        self._store = ExportStore(db)
        self.artifacts_root = artifacts_root
        self.workspace_root = workspace_root
        self.tex_compile_service = tex_compile_service
        self.artifact_blob_resolver = artifact_blob_resolver

    def latest_workspace_source_commit(self, problem_id: int, workspace_id: int) -> str:
        return self._store.latest_workspace_source_commit(problem_id, workspace_id)

    def download_source_commit(self, problem_id: int, workspace_id: int, verification_id: str, filename: str) -> str:
        return self._store.download_source_commit(int(problem_id), int(workspace_id), verification_id, filename)

    def workspace_exports(self, problem_id: int, workspace_id: int, *, limit: int) -> list[dict[str, object]]:
        return self._store.workspace_exports(int(problem_id), int(workspace_id), limit=limit)

    def export_archive_path(self, problem_id: int, workspace_id: int, export_id: str, problem_slug: str, filename: str) -> Path | None:
        row = self._store.export_archive_row(int(problem_id), int(workspace_id), export_id)
        if row is None:
            return None
        stored_filename = Path(str(row["filename"] or "").strip()).name
        archive_name = Path(str(filename or "").strip()).name
        if not stored_filename or stored_filename != archive_name:
            return None
        try:
            candidate = self._export_path(problem_slug, export_id, archive_name).resolve()
        except ValueError:
            return None
        if not candidate.exists() or not candidate.is_file() or candidate.is_symlink():
            return None
        return candidate

    def export_audit_rows(self, problem_id: int, actor_user_id: int, *, limit: int) -> list[dict[str, str]]:
        return self._store.export_audit_rows(int(problem_id), int(actor_user_id), limit=limit)

    def _yaml_quote(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

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

    def _find_first_source(self, folder: Path, preferred: list[str] | None = None) -> Path | None:
        if not folder.exists() or not folder.is_dir():
            return None
        try:
            _ = folder.resolve()
        except OSError:
            return None
        for name in preferred or []:
            p = folder / name
            if self._is_safe_regular_file(folder, p):
                return p

        best_name_by_suffix: dict[str, str] = {}
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    suffix = os.path.splitext(name)[1]
                    if suffix not in self.SOURCE_SUFFIX_ORDER:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    current = best_name_by_suffix.get(suffix)
                    if current is None or name < current:
                        best_name_by_suffix[suffix] = name
        except OSError:
            return None

        for suffix in self.SOURCE_SUFFIX_ORDER:
            selected = best_name_by_suffix.get(suffix)
            if selected:
                return folder / selected
        return None

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
        expected = infer_expected_behavior_from_name(f"solutions/{source_file.name}")
        desc_path = source_file.parent / f"{source_file.name}.desc"
        if self._is_safe_regular_file(source_file.parent, desc_path):
            try:
                payload = parse_solution_desc(desc_path.read_text(encoding="utf-8", errors="replace"))
                expected = normalize_expected_behavior(expected_behavior if isinstance(expected_behavior := payload.get("expected_behavior"), str) else expected)
            except OSError:
                pass
        return expected

    def _submission_dir_for_expected(self, expected_behavior: str) -> str | None:
        normalized = normalize_expected_behavior(expected_behavior)
        if normalized in self.KATTIS_SUBMISSION_DIRS:
            return normalized
        if normalized in {"tle_or_correct", "tle_or_re"}:
            return "time_limit_exceeded"
        return None

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

    def _load_build_config(self, snapshot: Path) -> dict:
        cfg_path = snapshot / "config" / "build.json"
        if not cfg_path.exists() or not cfg_path.is_file():
            return {}
        try:
            payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _resolve_snapshot_source(self, snapshot: Path, rel_path: str) -> Path:
        source_rel = str(rel_path or "").strip()
        if not source_rel:
            raise ValueError("configured source path is empty")
        resolved_snapshot = snapshot.resolve()
        source_path = (snapshot / source_rel).resolve()
        if resolved_snapshot not in source_path.parents:
            raise ValueError(f"invalid configured source path: {source_rel}")
        if source_path.is_symlink() or not source_path.exists() or not source_path.is_file():
            raise ValueError(f"configured source does not exist: {source_rel}")
        return source_path

    def _effective_checker_source(self, snapshot: Path, strict: bool) -> Path | None:
        build_cfg = self._load_build_config(snapshot)
        checker_source = checker_source.strip() if isinstance(checker_source := build_cfg.get("checker_source"), str) else ""
        if checker_source:
            try:
                return self._resolve_snapshot_source(snapshot, checker_source)
            except ValueError:
                if strict:
                    raise ValueError("checker_source is configured but invalid")
                return None
        return self._find_first_source(snapshot / "checkers")

    def _effective_interactor_source(self, snapshot: Path, strict: bool) -> Path | None:
        configured = interactor_source.strip() if isinstance(interactor_source := self._load_build_config(snapshot).get("interactor_source"), str) else ""
        if configured:
            try:
                return self._resolve_snapshot_source(snapshot, configured)
            except ValueError:
                if strict:
                    raise ValueError("interactor_source is configured but invalid")
                return None
        return self._find_first_source(snapshot / "interactors")

    def _problem_mode_and_pass_limit(self, snapshot: Path | None) -> tuple[str, int]:
        if snapshot is None:
            return ("pass-fail", 1)
        cfg_path = snapshot / "config" / "problem.json"
        if not cfg_path.exists():
            return ("pass-fail", 1)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        mode = str(cfg.get("mode") or "").strip()
        if mode not in {"pass-fail", "interactive"}:
            raise ValueError("export requires canonical problem.json mode")
        pass_limit_raw = cfg.get("pass_limit")
        try:
            pass_limit = int(pass_limit_raw)
        except Exception as exc:
            raise ValueError("export requires canonical problem.json pass_limit") from exc
        if pass_limit < 1:
            raise ValueError("export requires canonical positive pass_limit")
        return (mode, pass_limit)

    def _snapshot_source(
        self,
        workspace_id: int | None,
        problem_slug: str,
        source_commit: str | None,
        tmp_root: Path,
    ) -> Path:
        workspace = self._workspace_path_for_export(workspace_id, problem_slug)
        snapshot = tmp_root / "_source"
        source_commit = str(source_commit or "").strip()
        if source_commit:
            resolved = run_git(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "rev-parse",
                    "--verify",
                    f"{source_commit}^{{commit}}",
                ],
                timeout=120,
            )
            commit_sha = resolved.stdout.strip()
            if resolved.returncode != 0 or not commit_sha:
                detail = (resolved.stderr or resolved.stdout).strip()
                raise ValueError(
                    detail or f"unable to snapshot export source commit {source_commit}"
                )
            try:
                extract_git_archive(workspace, commit_sha, snapshot, timeout=120)
                remove_symlinks(snapshot)
                return snapshot
            except Exception as exc:
                shutil.rmtree(snapshot, ignore_errors=True)
                raise ValueError(str(exc))

        raise ValueError("export source snapshot requires non-empty source commit")

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
    ) -> Path:
        """Copy the workspace working tree to a temp snapshot using native-import path rules."""
        workspace = self._workspace_path_for_export(workspace_id, problem_slug)
        snapshot = tmp_root / "_source"
        snapshot.mkdir(parents=True, exist_ok=True)
        self._copy_native_working_tree(workspace, snapshot, root_dir=workspace)
        remove_symlinks(snapshot)
        return snapshot

    def _workspace_path_for_export(self, workspace_id: int | None, problem_slug: str) -> Path:
        if workspace_id is None:
            raise ValueError("build workspace metadata missing")
        ws_row = self._store.workspace_export_context(int(workspace_id))
        if ws_row is None:
            raise ValueError(f"workspace metadata not found: {workspace_id}")
        workspace = Path(ws_row["path"]).resolve()
        expected_workspace = (self.workspace_root / ws_row["username"] / problem_slug).resolve()
        if workspace != expected_workspace:
            raise ValueError(f"workspace path mismatch for export workspace {workspace_id}")
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError(f"workspace path missing for export workspace {workspace_id}")
        git_dir = workspace / ".git"
        if not git_dir.exists() or not git_dir.is_dir():
            raise ValueError(f"workspace git metadata missing for export workspace {workspace_id}")
        return workspace

    def _revision_number_for_commit(self, workspace: Path, source_commit: str) -> int | None:
        commit = str(source_commit or "").strip()
        if not commit:
            return None
        try:
            proc = run_git(
                ["git", "-C", str(workspace), "rev-list", "--count", commit],
                timeout=120,
            )
            if proc.returncode != 0:
                return None
            value = int(str(proc.stdout or "").strip())
            return value if value >= 0 else None
        except Exception:
            return None

    def _cleanup_previous_revision_exports(
        self,
        *,
        problem_slug: str,
        problem_id: int,
        workspace_id: int | None,
        export_type: str,
        source_commit: str,
        keep_export_id: str,
    ) -> None:
        if workspace_id is None:
            return
        rows = self._store.duplicate_exports(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            export_type=export_type,
            source_commit=source_commit,
            keep_export_id=keep_export_id,
        )
        for row in rows:
            old_id = row["id"]
            old_filename = row["filename"]
            if old_id and old_filename:
                try:
                    old_file = self._export_path(problem_slug, old_id, old_filename).resolve()
                    if old_file.exists() and old_file.is_file() and (not old_file.is_symlink()):
                        old_file.unlink()
                    old_dir = old_file.parent
                    if old_dir.exists() and old_dir.is_dir() and (not old_dir.is_symlink()):
                        old_dir.rmdir()
                except Exception:
                    pass
            if old_id:
                self._store.delete_export(old_id)

    def _load_problem_config(self, snapshot: Path) -> dict:
        cfg_path = snapshot / "config" / "problem.json"
        if not cfg_path.exists() or not cfg_path.is_file():
            return {}
        try:
            payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _build_problem_yaml(self, *, problem_name: str, mode: str, pass_limit: int, snapshot: Path, has_checker: bool) -> str:
        cfg = self._load_problem_config(snapshot)
        validation = "default"
        if str(mode or "").strip() == "interactive":
            validation = "custom interactive multi-pass" if int(pass_limit) > 1 else "custom interactive"
        elif has_checker:
            validation = "custom"
        lines: list[str] = []
        limit_lines: list[str] = []
        memory_limit_mb = cfg.get("memory_limit_mb")
        if isinstance(memory_limit_mb, int) and memory_limit_mb > 0:
            limit_lines.append(f"  memory: {memory_limit_mb}")
        if int(pass_limit) > 1:
            limit_lines.append(f"  validation_passes: {max(2, int(pass_limit))}")
        if limit_lines:
            lines.append("limits:")
            lines.extend(limit_lines)
        lines.extend(
            [
                f"name: {self._yaml_quote(str(problem_name or '').strip() or 'Problem')}",
                f"validation: {validation}",
            ]
        )
        return "\n".join(lines) + "\n"

    def _build_domjudge_problem_ini(self, *, slug: str, snapshot: Path) -> str:
        cfg = self._load_problem_config(snapshot)
        time_limit_ms = cfg.get("time_limit_ms")
        seconds = 2.0
        if isinstance(time_limit_ms, int) and time_limit_ms > 0:
            seconds = max(0.001, float(time_limit_ms) / 1000.0)
        short_name = Path(str(slug or "problem")).name[:32] or "problem"
        return (
            f"short-name = {short_name}\n"
            f"timelimit = {seconds:.3f}".rstrip("0").rstrip(".") + "\n"
            f"color = {self._domjudge_color(slug)}\n"
            f"externalid = {slug}\n"
        )

    def _statement_export_languages(self, snapshot: Path) -> list[str]:
        languages = statement_languages(snapshot)
        if languages:
            return languages
        return [pick_statement_language(snapshot)]

    @staticmethod
    def _statement_export_suffix(language: str) -> str:
        if language == "english":
            return "en"
        if language == "chinese":
            return "zh"
        return language

    def _try_compile_statement_pdf(self, snapshot: Path, dst_statement: Path, *, problem_name: str) -> bool:
        compiled_any = False
        for language in self._statement_export_languages(snapshot):
            try:
                rendered = render_statement_main(snapshot / "statement", problem_title=problem_name, language=language)
            except Exception:
                continue
            compile_result = self.tex_compile_service.compile_pdf(rendered)
            proc = compile_result.proc
            if proc.returncode != 0:
                continue
            pdf_path = compile_result.pdf_path
            if not pdf_path.exists() or not pdf_path.is_file():
                continue
            suffix = self._statement_export_suffix(language)
            dst_statement.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf_path, dst_statement / f"problem.{suffix}.pdf")
            compiled_any = True
        return compiled_any

    def _verification_blob(self, verification_id: str, test_id: str, ref_key: str) -> bytes | None:
        safe_verification_id = str(verification_id or "").strip()
        safe_test_id = str(test_id or "").strip()
        safe_ref_key = str(ref_key or "").strip()
        if safe_ref_key not in {"input_ref", "answer_ref"}:
            raise ValueError(f"invalid verification artifact ref: {safe_ref_key}")
        if not safe_verification_id or not safe_test_id:
            return None
        row = self.db.fetch_one(
            f"""
            SELECT {safe_ref_key}
            FROM verification_artifact_refs
            WHERE verification_id=? AND test_name=?
            """,
            [safe_verification_id, f"{safe_test_id}.in"],
        )
        if row is None:
            return None
        artifact_ref = str(row[safe_ref_key] or "")
        if not artifact_ref:
            return None
        return self.artifact_blob_resolver(artifact_ref)

    def _verification_input_blob(self, verification_id: str, test_id: str) -> bytes | None:
        return self._verification_blob(verification_id, test_id, "input_ref")

    def _verification_answer_blob(self, verification_id: str, test_id: str) -> bytes | None:
        return self._verification_blob(verification_id, test_id, "answer_ref")

    def _copy_workspace_payload_input(
        self,
        snapshot: Path,
        test_id: str,
        kind: str,
        target: Path,
    ) -> None:
        source_rel = payload_rel_path_for_test(test_id, kind)
        input_file = snapshot / source_rel
        if not self._is_safe_regular_file(snapshot, input_file):
            raise ValueError(f"export missing test input: {source_rel}")
        shutil.copy2(input_file, target)

    def _copy_required_test_input(
        self,
        snapshot: Path,
        row: dict,
        target: Path,
        *,
        verification_id: str,
    ) -> None:
        test_id = row["id"]
        if row["kind"] == "manual":
            self._copy_workspace_payload_input(snapshot, test_id, row["kind"], target)
            return
        input_blob = self._verification_input_blob(verification_id, test_id)
        if input_blob is None:
            raise ValueError(f"export missing verification input artifact: {test_id}.in")
        target.write_bytes(input_blob)

    def _copy_sample_input(
        self,
        snapshot: Path,
        row: dict,
        target: Path,
        *,
        verification_id: str,
    ) -> None:
        sample_input = row["sample_input"]
        if sample_input:
            target.write_text(sample_input, encoding="utf-8")
            return
        self._copy_required_test_input(snapshot, row, target, verification_id=verification_id)

    def _copy_required_answer(
        self,
        snapshot: Path,
        test_id: str,
        target: Path,
        *,
        verification_id: str,
    ) -> None:
        answer_blob = self._verification_answer_blob(verification_id, test_id)
        if answer_blob is not None:
            target.write_bytes(answer_blob)
            return
        raise ValueError(f"export missing verification answer artifact: {test_id}.ans")

    def _materialize_export_sample_display_payloads(
        self,
        snapshot: Path,
        *,
        verification_id: str,
    ) -> None:
        spec_path = snapshot / "tests" / "spec.json"
        entries = load_tests_spec(spec_path)
        changed = False
        for row in entries:
            if not bool(row["sample"]):
                continue
            if row["kind"] == "gen" and not row["sample_input"]:
                input_blob = self._verification_input_blob(verification_id, row["id"])
                if input_blob is not None:
                    row["sample_input"] = input_blob.decode("utf-8", errors="replace")
                    changed = True
            if row["sample_output"]:
                continue
            answer_blob = self._verification_answer_blob(verification_id, row["id"])
            if answer_blob is None:
                continue
            row["sample_output"] = answer_blob.decode("utf-8", errors="replace")
            changed = True
        if changed:
            spec_path.write_text(dumps_tests_spec(entries), encoding="utf-8")

    def _copy_secret_and_sample_data(
        self,
        snapshot: Path,
        package_root: Path,
        *,
        verification_id: str,
    ) -> None:
        secret_dir = package_root / "data" / "secret"
        sample_dir = package_root / "data" / "sample"
        secret_dir.mkdir(parents=True, exist_ok=True)
        sample_dir.mkdir(parents=True, exist_ok=True)
        entries = load_tests_spec(snapshot / "tests" / "spec.json")
        if not entries:
            raise ValueError("export requires tests/spec.json entries")
        for row in entries:
            test_id = row["id"]
            if not bool(row["sample"]):
                self._copy_required_test_input(
                    snapshot,
                    row,
                    secret_dir / f"{test_id}.in",
                    verification_id=verification_id,
                )
                self._copy_required_answer(
                    snapshot,
                    test_id,
                    secret_dir / f"{test_id}.ans",
                    verification_id=verification_id,
                )
                continue
            self._copy_sample_input(
                snapshot,
                row,
                sample_dir / f"{test_id}.in",
                verification_id=verification_id,
            )
            sample_output = row["sample_output"]
            if sample_output:
                (sample_dir / f"{test_id}.ans").write_text(sample_output, encoding="utf-8")
                continue
            self._copy_required_answer(
                snapshot,
                test_id,
                sample_dir / f"{test_id}.ans",
                verification_id=verification_id,
            )

    def _copy_output_validator_component(
        self,
        source: Path | None,
        dst_dir: Path,
        subdir: str,
        *,
        snapshot: Path,
        create_build_script: bool = False,
    ) -> None:
        if source is None:
            return
        target_dir = dst_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".cpp":
            testlib = snapshot / "third_party" / "testlib" / "testlib.h"
            if not self._is_safe_regular_file(snapshot, testlib):
                raise ValueError("export missing testlib header: third_party/testlib/testlib.h")
            shutil.copy2(testlib, target_dir / "testlib.h")
            if create_build_script:
                self._write_output_validator_build_script(target_dir, source.name)
        shutil.copy2(source, target_dir / source.name)

    def _write_output_validator_build_script(self, target_dir: Path, source_name: str) -> None:
        build = target_dir / "build"
        build.write_text(
            "#!/bin/sh\n"
            "# Auto-generated for DOMjudge multi-pass validation.\n"
            f"g++ -Wall -DDOMJUDGE -O2 {shlex.quote(source_name)} -std=gnu++20 -o run\n",
            encoding="utf-8",
        )
        build.chmod(0o755)

    def _copy_solutions(self, snapshot: Path, package_root: Path) -> None:
        dst_submissions = package_root / "submissions"
        dst_submissions.mkdir(parents=True, exist_ok=True)
        for source_file in self._iter_solution_sources(snapshot / "solutions"):
            expected = self._solution_expected_behavior(source_file)
            target_group = self._submission_dir_for_expected(expected)
            if not target_group:
                continue
            target_dir = dst_submissions / target_group
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, self._ensure_unique_file_path(target_dir, source_file.name))

    def _build_icpc_package(
        self,
        *,
        package_root: Path,
        snapshot: Path,
        problem_name: str,
        problem_slug: str,
        verification_id: str,
        mode: str,
        pass_limit: int,
    ) -> None:
        package_root.mkdir(parents=True, exist_ok=True)
        checker_source = None if mode == "interactive" else self._effective_checker_source(snapshot, strict=False)
        interactor_source = self._effective_interactor_source(snapshot, strict=False) if mode == "interactive" else None
        (package_root / "problem.yaml").write_text(
            self._build_problem_yaml(
                problem_name=problem_name,
                mode=mode,
                pass_limit=pass_limit,
                snapshot=snapshot,
                has_checker=checker_source is not None,
            ),
            encoding="utf-8",
        )
        (package_root / "domjudge-problem.ini").write_text(
            self._build_domjudge_problem_ini(slug=self._public_problem_slug(problem_slug), snapshot=snapshot),
            encoding="utf-8",
        )
        self._materialize_export_sample_display_payloads(snapshot, verification_id=verification_id)
        self._copy_secret_and_sample_data(snapshot, package_root, verification_id=verification_id)
        self._try_compile_statement_pdf(
            snapshot,
            package_root / "problem_statement",
            problem_name=problem_name,
        )
        self._copy_output_validator_component(
            interactor_source if mode == "interactive" else checker_source,
            package_root / "output_validators",
            "interactor" if mode == "interactive" else "checker",
            snapshot=snapshot,
            create_build_script=mode == "interactive" and int(pass_limit) > 1,
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
    ) -> Path:
        problem_row = self._store.problem_export_row(problem)
        if problem_row is None:
            raise ValueError(f"unknown problem: {problem}")
        snapshots_root = self.artifacts_root / "snapshots"
        snapshots_root.mkdir(parents=True, exist_ok=True)
        tmp_parent = snapshots_root / f"snap-{uuid.uuid4().hex[:12]}"
        tmp_root = tmp_parent / "work"
        package_root = tmp_root / self._package_root_name(str(problem_row["slug"]))
        try:
            package_root.mkdir(parents=True, exist_ok=True)
            snapshot = self._snapshot_working_tree(
                int(workspace_id),
                str(problem_row["slug"]),
                tmp_root,
            )
            self._build_native_package(
                package_root=package_root,
                snapshot=snapshot,
            )
            archive_stem = tmp_parent / f"{self._archive_filename_slug(str(problem_row['slug']))}-snapshot"
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
        return self.artifacts_root / "exports" / self._archive_filename_slug(problem_slug)

    def _export_dir(self, problem_slug: str, export_id: str) -> Path:
        safe_export_id = Path(str(export_id).strip()).name
        if not safe_export_id:
            raise ValueError("invalid export id")
        return self._export_problem_root(problem_slug) / safe_export_id

    def _export_path(self, problem_slug: str, export_id: str, filename: str) -> Path:
        safe_filename = Path(str(filename).strip()).name
        if not safe_filename:
            raise ValueError("invalid export filename")
        export_dir = self._export_dir(problem_slug, export_id)
        candidate = (export_dir / safe_filename).resolve()
        if export_dir.resolve() not in candidate.parents:
            raise ValueError("invalid export archive path")
        return candidate

    def create_export(
        self,
        problem: str,
        verification_id: str,
        export_type: str,
        *,
        workspace_id: int | None = None,
        source_commit: str = "",
    ) -> Path:
        resolved_export_type = str(export_type or "").strip().lower() or "icpc"
        resolved_verification_id = str(verification_id or "").strip()
        if resolved_export_type not in self.TYPES:
            raise ValueError("unsupported export type")

        problem_row = self._store.problem_export_row(problem)
        if problem_row is None:
            raise ValueError(f"unknown problem: {problem}")
        requested_source_commit = str(source_commit or "").strip()
        resolved_workspace_id = workspace_id
        if resolved_workspace_id is None:
            raise ValueError("export requires workspace_id")
        workspace = self._workspace_path_for_export(resolved_workspace_id, problem)
        if not requested_source_commit:
            head = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"], timeout=120)
            if head.returncode == 0 and head.stdout.strip():
                requested_source_commit = head.stdout.strip()
            else:
                raise ValueError("no committed revision; commit changes first")
        stored_source_commit = requested_source_commit
        export_id = f"e-{uuid.uuid4().hex[:10]}"
        export_dir = self._export_dir(str(problem_row["slug"]), export_id)
        export_dir.mkdir(parents=True, exist_ok=True)
        tmp_root = export_dir / f"tmp-{uuid.uuid4().hex[:8]}"
        package_root = tmp_root / self._package_root_name(problem_row["slug"])
        package_root.mkdir(parents=True, exist_ok=True)
        revision_number: int | None = None
        workspace_path: Path | None = None
        try:
            workspace_path = self._workspace_path_for_export(resolved_workspace_id, str(problem_row["slug"]))
        except Exception:
            workspace_path = None
        if workspace_path is not None:
            revision_number = self._revision_number_for_commit(workspace_path, stored_source_commit)
        revision_token = f"v{revision_number}" if isinstance(revision_number, int) and revision_number >= 0 else "v0"

        snapshot: Path | None = None
        try:
            mode = "pass-fail"
            pass_limit = 1
            if resolved_export_type == "icpc":
                snapshot = self._snapshot_source(
                    resolved_workspace_id,
                    str(problem_row["slug"]),
                    requested_source_commit,
                    tmp_root,
                )
                mode, pass_limit = self._problem_mode_and_pass_limit(snapshot)
                self._build_icpc_package(
                    package_root=package_root,
                    snapshot=snapshot,
                    problem_name=problem_row["name"],
                    problem_slug=str(problem_row["slug"]),
                    verification_id=resolved_verification_id,
                    mode=mode,
                    pass_limit=pass_limit,
                )
            else:
                snapshot = self._snapshot_source(
                    resolved_workspace_id,
                    str(problem_row["slug"]),
                    requested_source_commit,
                    tmp_root,
                )
                self._build_native_package(
                    package_root=package_root,
                    snapshot=snapshot,
                )

            if resolved_export_type == "native":
                preferred_filename = f"{self._archive_filename_slug(str(problem_row['slug']))}-native-{revision_token}.zip"
            else:
                preferred_filename = f"{self._public_problem_slug(str(problem_row['slug']))}-{revision_token}.zip"
            archive_target = self._export_path(str(problem_row["slug"]), export_id, preferred_filename)
            if resolved_export_type == "icpc":
                out = self._make_archive_from_dir_contents(archive_target, package_root)
            else:
                archive_prefix = archive_target.with_suffix("")
                archive = shutil.make_archive(
                    str(archive_prefix),
                    "zip",
                    root_dir=tmp_root,
                    base_dir=package_root.name,
                )
                out = Path(archive)
            digest = sha256_file(out)

            self._store.insert_export_record(
                export_id=export_id,
                problem_id=int(problem_row["id"]),
                verification_id=resolved_verification_id,
                workspace_id=resolved_workspace_id,
                export_type=resolved_export_type,
                filename=out.name,
                sha256=digest,
                size_bytes=int(out.stat().st_size),
                source_commit=stored_source_commit,
            )
            self._cleanup_previous_revision_exports(
                problem_slug=str(problem_row["slug"]),
                problem_id=int(problem_row["id"]),
                workspace_id=resolved_workspace_id,
                export_type=resolved_export_type,
                source_commit=stored_source_commit,
                keep_export_id=export_id,
            )
            return out
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
