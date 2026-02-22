from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.util import copytree, extract_git_archive, remove_symlinks, run_cmd, sha256_file


class ExportService:
    TYPES = {
        "kattis": "kattis.zip",
        "domjudge": "domjudge-legacy-icpc.zip",
        "polygon-standard": "polygon-standard.zip",
        "polygon-full": "polygon-full.zip",
    }
    STEP_LOGS = ["compile.log", "generate.log", "validate.log", "solve.log", "failure.log", "latex.log", "diagnostics.json"]

    def __init__(self, db: DB, artifacts_root: Path):
        self.db = db
        self.artifacts_root = artifacts_root

    def _canonical_build_root(self, problem: str, build_id: str) -> Path:
        aid = str(build_id or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", aid):
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
        try:
            resolved_root = root_resolved if root_resolved is not None else root.resolve()
        except OSError:
            return False
        try:
            resolved = p.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents or resolved_root == resolved

    def _iter_safe_descendant_files(self, root: Path):
        if not root.exists() or not root.is_dir():
            return
        root_resolved = root.resolve()
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            keep_dirs: list[str] = []
            for name in sorted(dirnames):
                d = dir_root / name
                if d.is_symlink():
                    continue
                try:
                    resolved = d.resolve()
                except OSError:
                    continue
                if root_resolved in resolved.parents or root_resolved == resolved:
                    keep_dirs.append(name)
            dirnames[:] = keep_dirs
            for name in sorted(filenames):
                p = dir_root / name
                if p.is_symlink():
                    continue
                try:
                    resolved = p.resolve()
                except OSError:
                    continue
                if root_resolved not in resolved.parents and root_resolved != resolved:
                    continue
                if not p.is_file():
                    continue
                yield p

    def _copy_path(self, src: Path, dst: Path) -> None:
        if not src.exists():
            return
        if src.is_symlink():
            return
        if src.is_dir():
            self._copy_dir_contents(src, dst)
            return
        if not src.is_file():
            return
        if not self._is_safe_regular_file(src.parent, src):
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

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
            folder_resolved = folder.resolve()
        except OSError:
            return None
        for name in preferred or []:
            p = folder / name
            if self._is_safe_regular_file(folder, p, root_resolved=folder_resolved):
                return p
        for pat in ["*.cpp", "*.cc", "*.cxx", "*.c", "*.py", "*.java"]:
            for p in sorted(folder.glob(pat)):
                if self._is_safe_regular_file(folder, p, root_resolved=folder_resolved):
                    return p
        return None

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

        for checker_src in sorted((snapshot / "checkers").glob("*")):
            if checker_src.is_file():
                try:
                    if "nextpass.in" in checker_src.read_text(encoding="utf-8", errors="ignore"):
                        return "multi-pass"
                except Exception:
                    continue
        return "pass-fail"

    def _snapshot_source(self, workspace_id: int | None, source_commit: str | None, tmp_root: Path) -> Path | None:
        if workspace_id is None:
            return None
        ws_row = self.db.fetch_one("SELECT path FROM workspaces WHERE id=?", [workspace_id])
        if ws_row is None:
            return None
        workspace = Path(ws_row["path"])
        if not workspace.exists() or not workspace.is_dir():
            return None

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

        copytree(workspace, snapshot)
        remove_symlinks(snapshot)
        return snapshot

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

        secret = data_root / "secret"
        sample = data_root / "sample"
        secret.mkdir(parents=True, exist_ok=True)
        sample.mkdir(parents=True, exist_ok=True)

        for t in sorted(tests_dir.glob("*.in")):
            if tests_dir_resolved is None or not self._is_safe_regular_file(
                tests_dir, t, root_resolved=tests_dir_resolved
            ):
                continue
            if first_test is None:
                first_test = t
            test_count += 1
            out_in = secret / t.name
            out_ans = secret / f"{t.stem}.ans"
            shutil.copy2(t, out_in)
            src_ans = ans_dir / f"{t.stem}.ans"
            if ans_dir_resolved is not None and self._is_safe_regular_file(
                ans_dir, src_ans, root_resolved=ans_dir_resolved
            ):
                shutil.copy2(src_ans, out_ans)
            else:
                out_ans.write_text("", encoding="utf-8")

        if first_test is None or test_count <= 0:
            raise ValueError("build has no tests to export")

        first = first_test
        shutil.copy2(first, sample / "1.in")
        first_ans = ans_dir / f"{first.stem}.ans"
        if ans_dir_resolved is not None and self._is_safe_regular_file(
            ans_dir, first_ans, root_resolved=ans_dir_resolved
        ):
            shutil.copy2(first_ans, sample / "1.ans")
        else:
            (sample / "1.ans").write_text("", encoding="utf-8")

    def _populate_submissions(self, snapshot: Path | None, submissions_dir: Path) -> None:
        accepted = submissions_dir / "accepted"
        accepted.mkdir(parents=True, exist_ok=True)

        copied = False
        if snapshot is not None:
            upstream_submissions = snapshot / "submissions" / "accepted"
            if upstream_submissions.exists():
                self._copy_dir_contents(upstream_submissions, accepted)
                copied = True

        if not copied and snapshot is not None:
            src = self._find_first_source(snapshot / "solutions", preferred=["accepted.cpp", "main.cpp"])
            if src is not None:
                shutil.copy2(src, accepted / src.name)
                copied = True

        if not copied:
            (accepted / "accepted.cpp").write_text(
                "#include <bits/stdc++.h>\n"
                "int main(){return 0;}\n",
                encoding="utf-8",
            )

    def _populate_input_validators(self, snapshot: Path | None, validators_dir: Path) -> None:
        validators_dir.mkdir(parents=True, exist_ok=True)
        if snapshot is not None:
            upstream = snapshot / "input_validators"
            if upstream.exists():
                self._copy_dir_contents(upstream, validators_dir)
            else:
                self._copy_dir_contents(snapshot / "validators", validators_dir)

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

    def _populate_output_validator(self, snapshot: Path | None, out_dir: Path, mode: str) -> None:
        if snapshot is None:
            return
        src: Path | None = None
        if mode == "interactive":
            src = self._find_first_source(snapshot / "interactors")
        if src is None:
            src = self._find_first_source(snapshot / "checkers")
        if src is None:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out_dir / src.name)

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

    def _write_domjudge_problem_yaml(self, package_root: Path, problem_name: str, source_commit: str | None, mode: str) -> None:
        validation = "default"
        if mode == "interactive":
            validation = "custom interactive"
        elif mode == "multi-pass":
            validation = "custom"
        lines = [
            "problem_format_version: legacy-icpc",
            f"name: {self._yaml_quote(problem_name)}",
            "license: unknown",
            f"validation: {self._yaml_quote(validation)}",
        ]
        if source_commit:
            lines.append(f"source: {self._yaml_quote(source_commit[:12])}")
        (package_root / "problem.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (package_root / "domjudge-problem.ini").write_text(
            f"short-name={self._package_root_name(problem_name)}\nname={problem_name}\n",
            encoding="utf-8",
        )

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

    def _build_domjudge(
        self,
        package_root: Path,
        build_root: Path,
        snapshot: Path | None,
        problem_name: str,
        source_commit: str | None,
        mode: str,
    ) -> None:
        self._write_domjudge_problem_yaml(package_root, problem_name, source_commit, mode)
        self._copy_statement(snapshot, build_root, package_root / "problem_statement")
        self._copy_test_data(build_root, package_root / "data")
        self._populate_submissions(snapshot, package_root / "submissions")
        self._populate_input_validators(snapshot, package_root / "input_validators")
        self._populate_output_validator(snapshot, package_root / "output_validators", mode)

    def _build_polygon(self, package_root: Path, build_root: Path, full: bool) -> None:
        self._copy_path(build_root / "manifest.json", package_root / "manifest.json")
        self._copy_path(build_root / "statement_preview", package_root / "statement_preview")

        logs_src = build_root / "logs"
        logs_dst = package_root / "logs"
        logs_dst.mkdir(parents=True, exist_ok=True)
        for name in self.STEP_LOGS:
            self._copy_path(logs_src / name, logs_dst / name)

        if full:
            self._copy_path(build_root / "tests", package_root / "tests")
            self._copy_path(build_root / "ans", package_root / "ans")

    def create_export(self, problem: str, build_id: str, export_type: str) -> Path:
        if export_type not in self.TYPES:
            raise ValueError("unsupported export type")

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
        if export_type in {"kattis", "domjudge"} and not source_commit:
            raise ValueError(f"build source_commit missing: {build_id}")

        build_root = self._canonical_build_root(problem, build_id)
        if not build_root.exists():
            raise ValueError(f"unknown build artifacts: {build_id}")
        required_paths = [build_root / "manifest.json", build_root / "logs"]
        if export_type in {"kattis", "domjudge", "polygon-full"}:
            required_paths.extend([build_root / "tests", build_root / "ans"])
        missing_paths = [str(p.relative_to(build_root)) for p in required_paths if not p.exists()]
        if missing_paths:
            raise ValueError(
                "incomplete build artifacts for export: " + ", ".join(sorted(missing_paths))
            )
        export_dir = build_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)

        base_name = self.TYPES[export_type].replace(".zip", "")
        export_id = f"e-{uuid.uuid4().hex[:10]}"
        tmp_root = export_dir / f"tmp-{uuid.uuid4().hex[:8]}"
        package_root = tmp_root / self._package_root_name(problem_row["slug"])
        package_root.mkdir(parents=True, exist_ok=True)

        snapshot: Path | None = None
        try:
            mode = "pass-fail"
            if export_type in {"kattis", "domjudge"}:
                snapshot = self._snapshot_source(build_row["workspace_id"], source_commit, tmp_root)
                mode = self._problem_mode(snapshot)

            if export_type == "kattis":
                self._build_kattis(
                    package_root=package_root,
                    build_root=build_root,
                    snapshot=snapshot,
                    problem_name=problem_row["name"],
                    source_commit=source_commit,
                    mode=mode,
                )
            elif export_type == "domjudge":
                self._build_domjudge(
                    package_root=package_root,
                    build_root=build_root,
                    snapshot=snapshot,
                    problem_name=problem_row["name"],
                    source_commit=source_commit,
                    mode=mode,
                )
            elif export_type == "polygon-standard":
                self._build_polygon(package_root, build_root, full=False)
            elif export_type == "polygon-full":
                self._build_polygon(package_root, build_root, full=True)

            archive_prefix = export_dir / f"{base_name}-{build_id}-{export_id}"
            archive = shutil.make_archive(
                str(archive_prefix),
                "zip",
                root_dir=tmp_root,
                base_dir=package_root.name,
            )
            out = Path(archive)
            digest = sha256_file(out)

            self.db.execute(
                "INSERT INTO exports(id,problem_id,build_id,export_type,filename,sha256,size_bytes,source_commit,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    export_id,
                    problem_row["id"],
                    build_id,
                    export_type,
                    out.name,
                    digest,
                    out.stat().st_size,
                    source_commit,
                    now_iso(),
                ],
            )
            return out
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
