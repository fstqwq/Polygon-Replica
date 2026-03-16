from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from app.db import DB
from app.service.disk.export_store import ExportStore
from app.service.platform.hashing import sha256_file
from app.service.platform.fs.op import extract_git_archive, remove_symlinks
from app.service.problem.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior, parse_solution_desc
from app.service.statement.render import render_statement_main
from app.service.platform.process import run_cmd


class ExportService:
    TYPES = {
        "icpc": "icpc.zip",
    }
    SOURCE_SUFFIX_ORDER = (".cpp", ".cc", ".cxx", ".c", ".py", ".java")
    KATTIS_SUBMISSION_DIRS = (
        "accepted",
        "wrong_answer",
        "time_limit_exceeded",
        "run_time_error",
        "rejected",
    )
    MODE_DETECT_READ_CHUNK = 65536
    STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[2] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
    STATEMENT_PDF_TIMEOUT_SEC = 60

    def __init__(self, db: DB, artifacts_root: Path, workspace_root: Path):
        self.db = db
        self._store = ExportStore(db)
        self.artifacts_root = artifacts_root
        self.workspace_root = workspace_root

    def latest_workspace_source_commit(self, problem_id: int, workspace_id: int) -> str:
        return self._store.latest_workspace_source_commit(problem_id, workspace_id)

    def download_source_commit(self, problem_id: int, workspace_id: int, verification_id: str, filename: str) -> str:
        return self._store.download_source_commit(int(problem_id), int(workspace_id), verification_id, filename)

    def workspace_exports(self, problem_id: int, workspace_id: int, *, limit: int) -> list[dict[str, object]]:
        return self._store.workspace_exports(int(problem_id), int(workspace_id), limit=limit)

    def export_audit_rows(self, problem_id: int, actor_user_id: int, *, limit: int) -> list[dict[str, str]]:
        return self._store.export_audit_rows(int(problem_id), int(actor_user_id), limit=limit)

    def _canonical_verification_root(self, artifact_path: str) -> Path:
        token = str(artifact_path or "").strip()
        if not token:
            raise ValueError("invalid artifact_path")
        try:
            base = self.artifacts_root.resolve()
            root = Path(token).resolve()
        except OSError as exc:
            raise ValueError("invalid artifact_path") from exc
        if root != base and base not in root.parents:
            raise ValueError("invalid artifact_path")
        return root

    def _yaml_quote(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _package_root_name(self, slug: str) -> str:
        return "".join(ch.lower() for ch in slug if ch.isalnum()) or "problem"

    def _archive_filename_slug(self, slug: str) -> str:
        token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(slug or "").strip())
        token = token.strip("-.")
        return token or "problem"

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

    def _validate_required_paths(self, build_root: Path, required: list[tuple[str, str]]) -> list[str]:
        try:
            root_resolved = build_root.resolve()
        except OSError:
            return [rel for rel, _kind in required]

        issues: list[str] = []
        for rel, kind in required:
            p = build_root / rel
            display = rel if kind == "file" else f"{rel}/"
            if not p.exists():
                issues.append(display)
                continue
            if p.is_symlink():
                issues.append(display)
                continue
            if kind == "dir":
                if not p.is_dir() or not self._is_safe_path_within(build_root, p, root_resolved=root_resolved):
                    issues.append(display)
                continue
            if not p.is_file() or not self._is_safe_path_within(build_root, p, root_resolved=root_resolved):
                issues.append(display)
        return issues

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

    def _iter_safe_top_level_suffix_files(
        self,
        folder: Path,
        suffix: str,
        folder_resolved: Path | None = None,
    ):
        if not suffix or not folder.exists() or not folder.is_dir():
            return
        # Require a resolvable root up front so callers keep deterministic empty behavior
        # for invalid/unreadable artifact directories.
        try:
            _ = folder_resolved if folder_resolved is not None else folder.resolve()
        except OSError:
            return
        matched: list[str] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    if not name.endswith(suffix):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    matched.append(name)
        except OSError:
            return
        for name in sorted(matched):
            yield folder / name

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

    def _effective_validator_source(self, snapshot: Path, strict: bool) -> Path | None:
        configured = validator_source.strip() if isinstance(validator_source := self._load_build_config(snapshot).get("validator_source"), str) else ""
        if configured:
            try:
                return self._resolve_snapshot_source(snapshot, configured)
            except ValueError:
                if strict:
                    raise ValueError("validator_source is configured but invalid")
                return None
        return self._find_first_source(snapshot / "validators")

    def _resolve_standard_checker_source(self, checker_standard: str) -> Path | None:
        raw = str(checker_standard or "").strip()
        if not raw:
            return None
        checker_name = raw[5:] if raw.startswith("std::") else raw
        checker_name = checker_name.strip()
        if not checker_name:
            raise ValueError("checker_standard is empty")
        if "/" in checker_name or "\\" in checker_name:
            raise ValueError("checker_standard is invalid")
        if not checker_name.endswith(".cpp"):
            checker_name += ".cpp"
        if not self.STANDARD_CHECKER_NAME_RE.fullmatch(checker_name):
            raise ValueError("checker_standard is invalid")
        source = (self.STANDARD_CHECKER_ROOT / checker_name).resolve()
        try:
            source.relative_to(self.STANDARD_CHECKER_ROOT)
        except ValueError as exc:
            raise ValueError("checker_standard is invalid") from exc
        try:
            if source.is_symlink() or not source.exists() or not source.is_file():
                raise ValueError(f"configured standard checker does not exist: std::{checker_name}")
        except OSError as exc:
            raise ValueError("standard checker catalog is unavailable") from exc
        return source

    def _effective_checker_source(self, snapshot: Path, strict: bool) -> Path | None:
        build_cfg = self._load_build_config(snapshot)
        checker_standard = checker_standard.strip() if isinstance(checker_standard := build_cfg.get("checker_standard"), str) else ""
        if checker_standard:
            source = self._resolve_standard_checker_source(checker_standard)
            if source is not None:
                return source
            if strict:
                raise ValueError("checker_standard is configured but invalid")
            return None
        checker_source = checker_source.strip() if isinstance(checker_source := build_cfg.get("checker_source"), str) else ""
        if checker_source:
            try:
                return self._resolve_snapshot_source(snapshot, checker_source)
            except ValueError:
                if strict:
                    raise ValueError("checker_source is configured but invalid")
                return None
        return self._find_first_source(snapshot / "checkers")

    def _file_contains_token(self, path: Path, token: str) -> bool:
        needle = str(token or "").encode("utf-8")
        if not needle:
            return False
        overlap = max(0, len(needle) - 1)
        carry = b""
        chunk_size = max(4096, int(self.MODE_DETECT_READ_CHUNK))
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    data = carry + chunk
                    if needle in data:
                        return True
                    carry = data[-overlap:] if overlap and len(data) > overlap else data if overlap else b""
        except OSError:
            return False
        return False

    def _problem_mode(self, snapshot: Path | None) -> str:
        allowed = {"pass-fail", "interactive", "multi-pass"}
        if snapshot is None:
            return "pass-fail"

        for rel in ["config/problem.json", "config/build.json"]:
            cfg_path = snapshot / rel
            if not cfg_path.exists():
                continue
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key in ["mode", "run_mode", "problem_type", "type"]:
                raw = cfg.get(key)
                values = [str(raw)] if isinstance(raw, str) else [str(x) for x in raw] if isinstance(raw, list) else []
                for v in values:
                    if v in allowed:
                        return v
        interactor_src = self._find_first_source(snapshot / "interactors")
        if interactor_src is not None:
            return "interactive"
        try:
            checker_src = self._effective_checker_source(snapshot, strict=False)
        except ValueError:
            checker_src = None
        if checker_src is not None and self._file_contains_token(checker_src, "nextpass.in"):
            return "multi-pass"
        return "pass-fail"

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
            resolved = run_cmd(
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
            proc = run_cmd(
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
            old_artifact_path = row["artifact_path"]
            if old_artifact_path and old_filename:
                try:
                    old_verification_root = self._canonical_verification_root(old_artifact_path)
                    export_root = (old_verification_root / "export").resolve()
                    old_file = (export_root / old_filename).resolve()
                    if (
                        export_root.exists()
                        and export_root.is_dir()
                        and (not export_root.is_symlink())
                        and old_file.exists()
                        and old_file.is_file()
                        and (not old_file.is_symlink())
                        and (export_root == old_file.parent or export_root in old_file.parents)
                    ):
                        old_file.unlink()
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

    def _problem_yaml_type(self, mode: str) -> str:
        token = str(mode or "pass-fail").strip().lower() or "pass-fail"
        if token == "interactive":
            return "[pass-fail, interactive]"
        if token == "multi-pass":
            return "[pass-fail, multi-pass]"
        return "pass-fail"

    def _build_problem_yaml(self, *, problem_name: str, mode: str, snapshot: Path) -> str:
        cfg = self._load_problem_config(snapshot)
        lines = [
            "problem_format_version: 2025-09",
            f"name: {self._yaml_quote(str(problem_name or '').strip() or 'Problem')}",
            f"type: {self._problem_yaml_type(mode)}",
        ]
        time_limit_ms = cfg.get("time_limit_ms")
        memory_limit_mb = cfg.get("memory_limit_mb")
        limit_lines: list[str] = []
        if isinstance(time_limit_ms, int) and time_limit_ms > 0:
            seconds = max(1, int(round(time_limit_ms / 1000.0)))
            limit_lines.append(f"  time_limit: {seconds} s")
        if isinstance(memory_limit_mb, int) and memory_limit_mb > 0:
            limit_lines.append(f"  memory_limit: {memory_limit_mb} MiB")
        if limit_lines:
            lines.append("limits:")
            lines.extend(limit_lines)
        return "\n".join(lines) + "\n"

    def _copy_statement_tree(self, snapshot: Path, dst_statement: Path, *, problem_name: str) -> None:
        dst_statement.mkdir(parents=True, exist_ok=True)
        src_statement = snapshot / "statement"
        if src_statement.exists() and src_statement.is_dir() and not src_statement.is_symlink():
            self._copy_dir_contents(src_statement, dst_statement)

        src_sections = snapshot / "statement-sections"
        if src_sections.exists() and src_sections.is_dir() and not src_sections.is_symlink():
            self._copy_dir_contents(src_sections, dst_statement.parent / "statement-sections")

        shutil.copy2(render_statement_main(snapshot / "statement", problem_title=problem_name), dst_statement / "problem.en.tex")

    def _try_compile_statement_pdf(self, snapshot: Path, dst_statement: Path) -> bool:
        try:
            rendered = render_statement_main(snapshot / "statement")
        except Exception:
            return False
        workdir = rendered.parent
        proc = run_cmd(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", rendered.name],
            cwd=workdir,
            timeout=self.STATEMENT_PDF_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            return False
        pdf_path = rendered.with_suffix(".pdf")
        if not pdf_path.exists() or not pdf_path.is_file():
            return False
        dst_statement.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, dst_statement / "problem.en.pdf")
        return True

    def _copy_secret_and_sample_data(self, build_root: Path, package_root: Path) -> None:
        tests_dir = build_root / "tests"
        ans_dir = build_root / "ans"
        secret_dir = package_root / "data" / "secret"
        sample_dir = package_root / "data" / "sample"
        secret_dir.mkdir(parents=True, exist_ok=True)
        sample_dir.mkdir(parents=True, exist_ok=True)
        input_files = list(self._iter_safe_top_level_suffix_files(tests_dir, ".in"))
        if not input_files:
            raise ValueError("verification artifacts do not contain test inputs")
        first_input = input_files[0]
        first_answer = ans_dir / f"{first_input.stem}.ans"
        shutil.copy2(first_input, sample_dir / "1.in")
        if self._is_safe_regular_file(ans_dir, first_answer):
            shutil.copy2(first_answer, sample_dir / "1.ans")
        for input_file in input_files:
            shutil.copy2(input_file, secret_dir / input_file.name)
            answer_file = ans_dir / f"{input_file.stem}.ans"
            if self._is_safe_regular_file(ans_dir, answer_file):
                shutil.copy2(answer_file, secret_dir / answer_file.name)

    def _copy_named_component(self, source: Path | None, dst_dir: Path) -> None:
        if source is None:
            return
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dst_dir / source.name)

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

    def _build_kattis(
        self,
        *,
        package_root: Path,
        build_root: Path,
        snapshot: Path,
        problem_name: str,
        mode: str,
    ) -> None:
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "problem.yaml").write_text(
            self._build_problem_yaml(problem_name=problem_name, mode=mode, snapshot=snapshot),
            encoding="utf-8",
        )
        self._copy_secret_and_sample_data(build_root, package_root)
        statement_dir = package_root / "statement"
        self._copy_statement_tree(snapshot, statement_dir, problem_name=problem_name)
        self._try_compile_statement_pdf(snapshot, statement_dir)
        validator_source = self._effective_validator_source(snapshot, strict=False)
        checker_source = None if mode == "interactive" else self._effective_checker_source(snapshot, strict=False)
        interactor_source = self._find_first_source(snapshot / "interactors") if mode == "interactive" else None
        self._copy_named_component(validator_source, package_root / "input_validators")
        self._copy_named_component(interactor_source if mode == "interactive" else checker_source, package_root / "output_validator")
        self._copy_solutions(snapshot, package_root)

    def create_export(self, problem: str, verification_id: str, export_type: str) -> Path:
        resolved_export_type = str(export_type or "").strip().lower() or "icpc"
        if resolved_export_type not in self.TYPES:
            raise ValueError("unsupported export type (ICPC only)")

        problem_row = self._store.problem_export_row(problem)
        if problem_row is None:
            raise ValueError(f"unknown problem: {problem}")

        verification_row = self._store.verification_export_row(verification_id)
        if verification_row is None:
            raise ValueError(f"verification metadata not found: {verification_id}")
        if verification_row["problem_id"] != problem_row["id"]:
            raise ValueError(f"verification {verification_id} does not belong to problem {problem}")
        if verification_row["status"] != "ok":
            raise ValueError(f"verification not exportable: {verification_id} (status={verification_row['status']})")
        source_commit = str(verification_row["source_commit"] or "").strip()
        if resolved_export_type == "icpc" and not source_commit:
            raise ValueError(f"verification source_commit missing: {verification_id}")
        artifact_path = str(verification_row["artifact_path"] or "").strip()
        if not artifact_path:
            raise ValueError(f"verification artifact_path missing: {verification_id}")

        verification_root = self._canonical_verification_root(artifact_path)
        if not verification_root.exists():
            raise ValueError(f"unknown verification artifacts: {verification_id}")
        required_paths: list[tuple[str, str]] = [("manifest.json", "file"), ("logs", "dir")]
        if resolved_export_type == "icpc":
            required_paths.extend([("tests", "dir"), ("ans", "dir")])
        missing_paths = self._validate_required_paths(verification_root, required_paths)
        if missing_paths:
            raise ValueError(
                "incomplete verification artifacts for export: " + ", ".join(sorted(missing_paths))
            )
        export_dir = verification_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)

        export_id = f"e-{uuid.uuid4().hex[:10]}"
        tmp_root = export_dir / f"tmp-{uuid.uuid4().hex[:8]}"
        package_root = tmp_root / self._package_root_name(problem_row["slug"])
        package_root.mkdir(parents=True, exist_ok=True)
        revision_number: int | None = None
        workspace_path: Path | None = None
        try:
            workspace_path = self._workspace_path_for_export(verification_row["workspace_id"], str(problem_row["slug"]))
        except Exception:
            workspace_path = None
        if workspace_path is not None:
            revision_number = self._revision_number_for_commit(workspace_path, source_commit)
        revision_token = f"v{revision_number}" if isinstance(revision_number, int) and revision_number >= 0 else "v0"

        snapshot: Path | None = None
        try:
            mode = "pass-fail"
            if resolved_export_type == "icpc":
                snapshot = self._snapshot_source(
                    verification_row["workspace_id"],
                    str(problem_row["slug"]),
                    source_commit,
                    tmp_root,
                )
                mode = self._problem_mode(snapshot)
                self._build_kattis(
                    package_root=package_root,
                    build_root=verification_root,
                    snapshot=snapshot,
                    problem_name=problem_row["name"],
                    mode=mode,
                )

            preferred_filename = f"{self._archive_filename_slug(str(problem_row['slug']))}-{revision_token}.zip"
            archive_target = export_dir / preferred_filename
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
                verification_id=verification_id,
                workspace_id=verification_row["workspace_id"],
                export_type=resolved_export_type,
                filename=out.name,
                sha256=digest,
                size_bytes=int(out.stat().st_size),
                source_commit=source_commit,
            )
            self._cleanup_previous_revision_exports(
                problem_id=int(problem_row["id"]),
                workspace_id=verification_row["workspace_id"],
                export_type=resolved_export_type,
                source_commit=source_commit,
                keep_export_id=export_id,
            )
            return out
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
