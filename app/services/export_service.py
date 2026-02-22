from __future__ import annotations

import json
import shlex
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.util import copytree, run_cmd, sha256_file


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

    def _yaml_quote(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _package_root_name(self, slug: str) -> str:
        out = "".join(ch.lower() for ch in slug if ch.isalnum())
        return out or "problem"

    def _copy_path(self, src: Path, dst: Path) -> None:
        if not src.exists():
            return
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _copy_dir_contents(self, src: Path, dst: Path) -> None:
        if not src.exists() or not src.is_dir():
            return
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            target = dst / rel
            if p.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)

    def _find_first_source(self, folder: Path, preferred: list[str] | None = None) -> Path | None:
        if not folder.exists() or not folder.is_dir():
            return None
        for name in preferred or []:
            p = folder / name
            if p.exists() and p.is_file():
                return p
        matches: list[Path] = []
        for pat in ["*.cpp", "*.cc", "*.cxx", "*.c", "*.py", "*.java"]:
            matches.extend(sorted(folder.glob(pat)))
        return matches[0] if matches else None

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

    def _snapshot_source(self, build_row, tmp_root: Path) -> Path | None:
        workspace_id = build_row["workspace_id"]
        if workspace_id is None:
            return None
        ws_row = self.db.fetch_one("SELECT path FROM workspaces WHERE id=?", [workspace_id])
        if ws_row is None:
            return None
        workspace = Path(ws_row["path"])
        if not workspace.exists():
            return None

        snapshot = tmp_root / "_source"
        source_commit = str(build_row["source_commit"] or "").strip()
        if source_commit:
            snapshot.mkdir(parents=True, exist_ok=True)
            cmd = (
                "set -euo pipefail; "
                f"git -C {shlex.quote(str(workspace))} archive {shlex.quote(source_commit)} "
                f"| tar -x -C {shlex.quote(str(snapshot))}"
            )
            proc = run_cmd(["bash", "-lc", cmd], timeout=120)
            if proc.returncode == 0:
                return snapshot
            shutil.rmtree(snapshot, ignore_errors=True)

        copytree(workspace, snapshot)
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
        tests = sorted(tests_dir.glob("*.in"))
        if not tests:
            raise ValueError("build has no tests to export")

        secret = data_root / "secret"
        sample = data_root / "sample"
        secret.mkdir(parents=True, exist_ok=True)
        sample.mkdir(parents=True, exist_ok=True)

        for t in tests:
            out_in = secret / t.name
            out_ans = secret / f"{t.stem}.ans"
            shutil.copy2(t, out_in)
            src_ans = ans_dir / f"{t.stem}.ans"
            if src_ans.exists():
                shutil.copy2(src_ans, out_ans)
            else:
                out_ans.write_text("", encoding="utf-8")

        first = tests[0]
        shutil.copy2(first, sample / "1.in")
        first_ans = ans_dir / f"{first.stem}.ans"
        if first_ans.exists():
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

        if not any(p.is_file() for p in validators_dir.rglob("*")):
            (validators_dir / "validator.cpp").write_text(
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
            "SELECT problem_id,workspace_id,source_commit FROM builds WHERE id=?",
            [build_id],
        )
        if build_row is None:
            raise ValueError(f"build metadata not found: {build_id}")
        if build_row["problem_id"] != problem_row["id"]:
            raise ValueError(f"build {build_id} does not belong to problem {problem}")

        build_root = self.artifacts_root / problem / build_id
        if not build_root.exists():
            raise ValueError(f"unknown build artifacts: {build_id}")
        export_dir = build_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)

        base_name = self.TYPES[export_type].replace(".zip", "")
        tmp_root = export_dir / f"tmp-{uuid.uuid4().hex[:8]}"
        package_root = tmp_root / self._package_root_name(problem_row["slug"])
        package_root.mkdir(parents=True, exist_ok=True)

        source_commit = build_row["source_commit"]
        snapshot: Path | None = None
        try:
            snapshot = self._snapshot_source(build_row, tmp_root)
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

            archive = shutil.make_archive(
                str(export_dir / base_name),
                "zip",
                root_dir=tmp_root,
                base_dir=package_root.name,
            )
            out = Path(archive)
            digest = sha256_file(out)

            self.db.execute(
                "INSERT INTO exports(id,problem_id,build_id,export_type,filename,sha256,size_bytes,source_commit,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    f"e-{uuid.uuid4().hex[:10]}",
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
