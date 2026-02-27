from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior, parse_solution_desc
from app.services.util import extract_git_archive, is_canonical_artifact_id, remove_symlinks, run_cmd, sha256_file


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

    def __init__(self, db: DB, artifacts_root: Path, workspace_root: Path):
        self.db = db
        self.artifacts_root = artifacts_root
        self.workspace_root = workspace_root

    def _canonical_build_root(self, problem: str, build_id: str) -> Path:
        aid = str(build_id or "")
        if not is_canonical_artifact_id(aid):
            raise ValueError("invalid build artifact id")
        base = (self.artifacts_root / problem).resolve()
        root = (base / aid).resolve()
        try:
            rel = root.relative_to(base)
        except ValueError as exc:
            raise ValueError("invalid build artifact id") from exc
        if len(rel.parts) != 1 or rel.parts[0] != aid:
            raise ValueError("invalid build artifact id")
        return root

    def _yaml_quote(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _package_root_name(self, slug: str) -> str:
        out = "".join(ch.lower() for ch in slug if ch.isalnum())
        return out or "problem"

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
                expected = normalize_expected_behavior(str(payload.get("expected_behavior") or expected))
            except OSError:
                pass
        return expected

    def _submission_dir_for_expected(self, expected_behavior: str) -> str | None:
        normalized = normalize_expected_behavior(expected_behavior)
        if normalized in self.KATTIS_SUBMISSION_DIRS:
            return normalized
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
        build_cfg = self._load_build_config(snapshot)
        configured = str(build_cfg.get("validator_source") or "").strip()
        if configured:
            try:
                return self._resolve_snapshot_source(snapshot, configured)
            except ValueError:
                if strict:
                    raise ValueError("validator_source is configured but invalid")
                return None
        return self._find_first_source(snapshot / "validators")

    def _normalize_standard_checker_name(self, raw: str) -> str:
        value = str(raw or "").strip()
        if value.startswith("std::"):
            value = value[5:]
        if not value:
            raise ValueError("checker_standard is empty")
        if "/" in value or "\\" in value:
            raise ValueError("checker_standard is invalid")
        if not value.endswith(".cpp"):
            value += ".cpp"
        if not self.STANDARD_CHECKER_NAME_RE.fullmatch(value):
            raise ValueError("checker_standard is invalid")
        return value

    def _resolve_standard_checker_source(self, checker_standard: str) -> Path | None:
        raw = str(checker_standard or "").strip()
        if not raw:
            return None
        checker_name = self._normalize_standard_checker_name(raw)
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
        checker_standard = str(build_cfg.get("checker_standard") or "").strip()
        if checker_standard:
            source = self._resolve_standard_checker_source(checker_standard)
            if source is not None:
                return source
            if strict:
                raise ValueError("checker_standard is configured but invalid")
            return None
        checker_source = str(build_cfg.get("checker_source") or "").strip()
        if checker_source:
            try:
                return self._resolve_snapshot_source(snapshot, checker_source)
            except ValueError:
                if strict:
                    raise ValueError("checker_source is configured but invalid")
                return None
        return self._find_first_source(snapshot / "checkers")

    def _is_source_filename(self, filename: str) -> bool:
        return Path(str(filename or "")).suffix.lower() in self.SOURCE_SUFFIX_ORDER

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
        ws_row = self.db.fetch_one(
            """
            SELECT w.user_id,w.path,u.username
            FROM workspaces w
            JOIN users u ON u.id=w.user_id
            WHERE w.id=?
            """,
            [workspace_id],
        )
        if ws_row is None:
            raise ValueError(f"workspace metadata not found: {workspace_id}")
        workspace = Path(str(ws_row["path"] or "")).resolve()
        expected_workspace = (self.workspace_root / str(ws_row["username"]) / problem_slug).resolve()
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
        problem_slug: str,
        problem_id: int,
        workspace_id: int | None,
        export_type: str,
        source_commit: str,
        keep_export_id: str,
    ) -> None:
        if workspace_id is None:
            return
        rows = self.db.fetch_all(
            """
            SELECT id,build_id,filename
            FROM exports
            WHERE problem_id=? AND workspace_id=? AND export_type=? AND source_commit=? AND id<>?
            ORDER BY created_at DESC
            """,
            [problem_id, workspace_id, export_type, source_commit, keep_export_id],
        )
        for row in rows:
            old_id = str(row["id"] or "").strip()
            old_build_id = str(row["build_id"] or "").strip()
            old_filename = str(row["filename"] or "").strip()
            if old_build_id and old_filename:
                try:
                    old_build_root = self._canonical_build_root(problem_slug, old_build_id)
                    old_file = (old_build_root / "export" / old_filename).resolve()
                    export_root = (old_build_root / "export").resolve()
                    if export_root in old_file.parents and old_file.exists() and old_file.is_file():
                        old_file.unlink(missing_ok=True)
                except Exception:
                    pass
            if old_id:
                try:
                    self.db.execute("DELETE FROM exports WHERE id=?", [old_id])
                except Exception:
                    pass

    def _copy_statement(self, snapshot: Path | None, build_root: Path, dst_statement: Path) -> None:
        if snapshot is not None:
            self._copy_dir_contents(snapshot / "statement", dst_statement)

        has_problem_statement = any(dst_statement.glob("problem.*.tex")) or any(dst_statement.glob("problem.*.pdf"))
        if not has_problem_statement:
            main_tex = dst_statement / "main.tex"
            main_pdf = dst_statement / "main.pdf"
            if main_tex.exists():
                shutil.copy2(main_tex, dst_statement / "problem.en.tex")
                has_problem_statement = True
            elif main_pdf.exists():
                shutil.copy2(main_pdf, dst_statement / "problem.en.pdf")
                has_problem_statement = True

        if not has_problem_statement:
            preview_pdf = build_root / "statement_preview" / "statement.pdf"
            if preview_pdf.exists():
                dst_statement.mkdir(parents=True, exist_ok=True)
                shutil.copy2(preview_pdf, dst_statement / "problem.en.pdf")
                has_problem_statement = True

        if not has_problem_statement:
            dst_statement.mkdir(parents=True, exist_ok=True)
            (dst_statement / "problem.en.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "Statement is unavailable in this export snapshot.\n"
                "\\end{document}\n",
                encoding="utf-8",
            )

    def _copy_test_data(self, build_root: Path, data_root: Path) -> None:
        tests_dir = build_root / "tests"
        ans_dir = build_root / "ans"
        try:
            tests_dir_resolved = tests_dir.resolve()
        except OSError:
            tests_dir_resolved = None
        first_test: Path | None = None
        test_count = 0
        try:
            ans_dir_resolved = ans_dir.resolve()
        except OSError:
            ans_dir_resolved = None
        safe_answers: dict[str, Path] = {}
        if ans_dir_resolved is not None:
            for ap in self._iter_safe_top_level_suffix_files(
                ans_dir,
                ".ans",
                folder_resolved=ans_dir_resolved,
            ):
                safe_answers[ap.name] = ap

        secret = data_root / "secret"
        sample = data_root / "sample"
        secret.mkdir(parents=True, exist_ok=True)
        sample.mkdir(parents=True, exist_ok=True)

        for t in self._iter_safe_top_level_suffix_files(
            tests_dir,
            ".in",
            folder_resolved=tests_dir_resolved,
        ):
            if first_test is None:
                first_test = t
            test_count += 1
            out_in = secret / t.name
            out_ans = secret / f"{t.stem}.ans"
            shutil.copy2(t, out_in)
            src_ans = safe_answers.get(f"{t.stem}.ans")
            if src_ans is not None:
                shutil.copy2(src_ans, out_ans)
            else:
                out_ans.write_text("", encoding="utf-8")

        if first_test is None or test_count <= 0:
            raise ValueError("build has no tests to export")

        first = first_test
        shutil.copy2(first, sample / "1.in")
        first_ans = safe_answers.get(f"{first.stem}.ans")
        if first_ans is not None:
            shutil.copy2(first_ans, sample / "1.ans")
        else:
            (sample / "1.ans").write_text("", encoding="utf-8")

    def _populate_submissions(self, snapshot: Path | None, submissions_dir: Path) -> None:
        for folder in self.KATTIS_SUBMISSION_DIRS:
            (submissions_dir / folder).mkdir(parents=True, exist_ok=True)

        copied_any = False
        if snapshot is not None:
            upstream_root = snapshot / "submissions"
            for src in self._iter_safe_descendant_files(upstream_root):
                try:
                    rel = src.relative_to(upstream_root)
                except ValueError:
                    continue
                if len(rel.parts) < 2:
                    continue
                mapped_group = self._submission_dir_for_expected(rel.parts[0])
                if not mapped_group:
                    continue
                dst = submissions_dir / mapped_group
                for part in rel.parts[1:]:
                    dst = dst / part
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied_any = True

        if snapshot is not None:
            solutions_dir = snapshot / "solutions"
            for src in self._iter_solution_sources(solutions_dir):
                expected = self._solution_expected_behavior(src)
                mapped_group = self._submission_dir_for_expected(expected)
                if not mapped_group:
                    continue
                dst_dir = submissions_dir / mapped_group
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = self._ensure_unique_file_path(dst_dir, src.name)
                shutil.copy2(src, dst)
                copied_any = True

        accepted = submissions_dir / "accepted"
        has_accepted = next(self._iter_safe_descendant_files(accepted), None) is not None
        if not has_accepted and snapshot is not None:
            src = self._find_first_source(snapshot / "solutions", preferred=["accepted.cpp", "main.cpp"])
            if src is not None:
                dst = self._ensure_unique_file_path(accepted, src.name)
                shutil.copy2(src, dst)
                has_accepted = True
                copied_any = True

        if not has_accepted:
            (accepted / "accepted.cpp").write_text(
                "#include <bits/stdc++.h>\n"
                "int main(){return 0;}\n",
                encoding="utf-8",
            )
            copied_any = True

        if not copied_any:
            # Fallback should be unreachable because accepted placeholder is always written,
            # but keep an explicit guard for future edits.
            (accepted / "accepted.cpp").write_text(
                "#include <bits/stdc++.h>\n"
                "int main(){return 0;}\n",
                encoding="utf-8",
            )

    def _populate_input_validators(self, snapshot: Path | None, validators_dir: Path) -> None:
        validators_dir.mkdir(parents=True, exist_ok=True)
        selected_rel: Path | None = None
        if snapshot is not None:
            upstream = snapshot / "input_validators"
            if upstream.exists():
                self._copy_dir_contents(upstream, validators_dir)
            else:
                validators_root = snapshot / "validators"
                selected_src = self._effective_validator_source(snapshot, strict=False)
                if validators_root.exists():
                    self._copy_dir_contents(validators_root, validators_dir)
                if selected_src is not None:
                    try:
                        selected_rel = selected_src.resolve().relative_to(validators_root.resolve())
                    except Exception:
                        selected_rel = Path(selected_src.name)
                        target = validators_dir / selected_rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(selected_src, target)

        if selected_rel is not None:
            for candidate in list(self._iter_safe_descendant_files(validators_dir)):
                rel = candidate.relative_to(validators_dir)
                if not self._is_source_filename(rel.name):
                    continue
                if rel == selected_rel:
                    continue
                candidate.unlink(missing_ok=True)

        has_validator = next(self._iter_safe_descendant_files(validators_dir), None) is not None
        if not has_validator:
            fallback = validators_dir / "validator.cpp"
            if fallback.is_symlink():
                fallback.unlink(missing_ok=True)
            elif fallback.exists() and not fallback.is_file():
                if fallback.is_dir():
                    shutil.rmtree(fallback, ignore_errors=True)
                else:
                    fallback.unlink(missing_ok=True)
            fallback.write_text(
                "#include <bits/stdc++.h>\n"
                "int main(){return 42;}\n",
                encoding="utf-8",
            )

    def _populate_output_validator(self, snapshot: Path | None, out_dir: Path, mode: str) -> bool:
        if snapshot is None:
            return False
        src: Path | None = None
        if mode == "interactive":
            src = self._find_first_source(snapshot / "interactors")
        if src is None:
            src = self._effective_checker_source(snapshot, strict=True)
        if src is None:
            return False
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out_dir / src.name)
        return True

    def _kattis_type_yaml(self, mode: str) -> str:
        if mode == "interactive":
            return "[pass-fail, interactive]"
        if mode == "multi-pass":
            return "[pass-fail, multi-pass]"
        return "pass-fail"

    def _write_kattis_problem_yaml(self, package_root: Path, problem_name: str, source_commit: str | None, mode: str) -> None:
        pkg_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"polygonlike:{problem_name}:{source_commit or 'head'}")
        lines = [
            "problem_format_version: 2025-09",
            f"name: {self._yaml_quote(problem_name)}",
            f"uuid: {self._yaml_quote(str(pkg_uuid))}",
            f"type: {self._kattis_type_yaml(mode)}",
            "license: unknown",
        ]
        if source_commit:
            lines.append(f"version: {self._yaml_quote(source_commit[:12])}")
        (package_root / "problem.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _build_kattis(
        self,
        package_root: Path,
        build_root: Path,
        snapshot: Path | None,
        problem_name: str,
        source_commit: str | None,
        mode: str,
    ) -> None:
        self._write_kattis_problem_yaml(package_root, problem_name, source_commit, mode)
        self._copy_statement(snapshot, build_root, package_root / "statement")
        self._copy_test_data(build_root, package_root / "data")
        self._populate_submissions(snapshot, package_root / "submissions")
        self._populate_input_validators(snapshot, package_root / "input_validators")
        self._populate_output_validator(snapshot, package_root / "output_validator", mode)

    def create_export(self, problem: str, build_id: str, export_type: str) -> Path:
        export_type_raw = str(export_type or "").strip().lower()
        resolved_export_type = export_type_raw or "icpc"
        if resolved_export_type not in self.TYPES:
            raise ValueError("unsupported export type (ICPC only)")

        problem_row = self.db.fetch_one("SELECT id,slug,name FROM problems WHERE slug=?", [problem])
        if problem_row is None:
            raise ValueError(f"unknown problem: {problem}")

        build_row = self.db.fetch_one(
            "SELECT problem_id,workspace_id,source_commit,status FROM builds WHERE id=?",
            [build_id],
        )
        if build_row is None:
            raise ValueError(f"build metadata not found: {build_id}")
        if build_row["problem_id"] != problem_row["id"]:
            raise ValueError(f"build {build_id} does not belong to problem {problem}")
        if build_row["status"] != "ok":
            raise ValueError(f"build not exportable: {build_id} (status={build_row['status']})")
        source_commit = str(build_row["source_commit"] or "").strip()
        if resolved_export_type == "icpc" and not source_commit:
            raise ValueError(f"build source_commit missing: {build_id}")

        build_root = self._canonical_build_root(problem, build_id)
        if not build_root.exists():
            raise ValueError(f"unknown build artifacts: {build_id}")
        required_paths: list[tuple[str, str]] = [("manifest.json", "file"), ("logs", "dir")]
        if resolved_export_type == "icpc":
            required_paths.extend([("tests", "dir"), ("ans", "dir")])
        missing_paths = self._validate_required_paths(build_root, required_paths)
        if missing_paths:
            raise ValueError(
                "incomplete build artifacts for export: " + ", ".join(sorted(missing_paths))
            )
        export_dir = build_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)

        export_id = f"e-{uuid.uuid4().hex[:10]}"
        tmp_root = export_dir / f"tmp-{uuid.uuid4().hex[:8]}"
        package_root = tmp_root / self._package_root_name(problem_row["slug"])
        package_root.mkdir(parents=True, exist_ok=True)
        revision_number: int | None = None
        workspace_path: Path | None = None
        try:
            workspace_path = self._workspace_path_for_export(build_row["workspace_id"], str(problem_row["slug"]))
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
                    build_row["workspace_id"],
                    str(problem_row["slug"]),
                    source_commit,
                    tmp_root,
                )
                mode = self._problem_mode(snapshot)
                self._build_kattis(
                    package_root=package_root,
                    build_root=build_root,
                    snapshot=snapshot,
                    problem_name=problem_row["name"],
                    source_commit=source_commit,
                    mode=mode,
                )

            preferred_filename = f"{problem_row['slug']}-{revision_token}.zip"
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

            self.db.execute(
                "INSERT INTO exports(id,problem_id,build_id,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    export_id,
                    problem_row["id"],
                    build_id,
                    build_row["workspace_id"],
                    resolved_export_type,
                    out.name,
                    digest,
                    out.stat().st_size,
                    source_commit,
                    now_iso(),
                ],
            )
            self._cleanup_previous_revision_exports(
                problem_slug=str(problem_row["slug"]),
                problem_id=int(problem_row["id"]),
                workspace_id=build_row["workspace_id"],
                export_type=resolved_export_type,
                source_commit=source_commit,
                keep_export_id=export_id,
            )
            return out
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
