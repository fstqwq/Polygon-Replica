#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path


def ensure_local_env() -> None:
    os.environ.setdefault("POLYGONLIKE_DB", "./var/polygonlike.db")
    os.environ.setdefault("POLYGONLIKE_BARE_ROOT", "./var/srv/git")
    os.environ.setdefault("POLYGONLIKE_WORKSPACE_ROOT", "./var/srv/workspaces")
    os.environ.setdefault("POLYGONLIKE_RUN_ROOT", "./var/srv/runs")
    os.environ.setdefault("POLYGONLIKE_ARTIFACTS_ROOT", "./var/lib/polygonlike/artifacts")
    os.environ.setdefault("POLYGONLIKE_CACHE_ROOT", "./var/cache/polygonlike")


def _zip_entries(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def _has_suffix(entries: list[str], suffix: str) -> bool:
    target = suffix.strip("/")
    for name in entries:
        n = name.rstrip("/")
        if n == target or n.endswith("/" + target):
            return True
    return False


def _expect_suffix(entries: list[str], suffix: str, label: str) -> None:
    if not _has_suffix(entries, suffix):
        raise RuntimeError(f"{label} missing required path: {suffix}")


def _expect_absent_fragment(entries: list[str], fragment: str, label: str) -> None:
    for name in entries:
        if fragment in name:
            raise RuntimeError(f"{label} should not contain: {fragment}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    ensure_local_env()

    from fastapi.testclient import TestClient
    from app.main import app, build_service, db, export_service, preview_service, run_service, workspace_service

    with TestClient(app) as client:
        for path in [
            "/problems/sample/alice/files",
            "/problems/sample/alice/git",
            "/problems/sample/alice/build",
            "/problems/sample/alice/preview",
            "/problems/sample/alice/run",
            "/problems/sample/alice/export",
            "/api/problems/sample/workspaces/alice/status",
            "/api/problems/sample/workspaces/alice/branches",
            "/api/problems/sample/workspaces/alice/recent-builds",
            "/api/problems/sample/workspaces/alice/recent-previews",
            "/api/problems/sample/workspaces/alice/recent-runs",
            "/api/problems/sample/workspaces/alice/recent-exports",
        ]:
            r = client.get(path)
            if r.status_code != 200:
                raise RuntimeError(f"endpoint failed: {path} status={r.status_code}")

    ctx = workspace_service.workspace_context("sample", "alice")
    ws = Path(ctx["workspace"]["path"])

    for d in ["solutions", "validators", "checkers", "generators", "tests/manual", "config", "statement"]:
        (ws / d).mkdir(parents=True, exist_ok=True)

    (ws / "config/build.json").write_text(json.dumps({"generator_runs": 1}), encoding="utf-8")
    (ws / "tests/manual/001.in").write_text("1\n", encoding="utf-8")
    (ws / "solutions/accepted.cpp").write_text(
        '#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 0; cout<<x<<"\\n";}',
        encoding="utf-8",
    )
    (ws / "solutions/main.cpp").write_text(
        '#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 0; cout<<x<<"\\n";}',
        encoding="utf-8",
    )
    (ws / "validators/validator.cpp").write_text(
        "#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 1; if(x<0) return 2; string rest; getline(cin,rest); return 0;}",
        encoding="utf-8",
    )
    (ws / "checkers/checker.cpp").write_text(
        "#include <bits/stdc++.h>\n#include <filesystem>\nusing namespace std; int main(int argc,char** argv){ifstream out(argv[2]); long long a=0; if(!(out>>a)) return 1; const char* fb=getenv(\"FEEDBACK_DIR\"); if(fb){ filesystem::create_directories(fb); ofstream(string(fb)+\"/judgemessage.txt\")<<\"judge\"; ofstream(string(fb)+\"/teammessage.txt\")<<\"team\"; if(a>0){ ofstream(string(fb)+\"/nextpass.in\")<<(a-1)<<\"\\n\"; } } return a>=0?0:1; }",
        encoding="utf-8",
    )
    (ws / "interactors/interactor.cpp").write_text(
        "#include <bits/stdc++.h>\n#include <filesystem>\nusing namespace std; int main(int argc,char** argv){ if(argc<2) return 1; ifstream in(argv[1]); long long x=0; if(!(in>>x)) return 2; cout<<x<<endl; long long y=0; if(!(cin>>y)) return 3; const char* fb=getenv(\"FEEDBACK_DIR\"); if(fb){ filesystem::create_directories(fb); ofstream(string(fb)+\"/judgemessage.txt\")<<\"judge\"; ofstream(string(fb)+\"/teammessage.txt\")<<\"team\"; } return x==y?0:1; }",
        encoding="utf-8",
    )
    (ws / "generators/gen.cpp").write_text('#include <bits/stdc++.h>\nint main(){std::cout<<1<<"\\n";}', encoding="utf-8")
    (ws / "statement/main.tex").write_text("\\documentclass{article}\\begin{document}ok\\end{document}\n", encoding="utf-8")

    preview_id = preview_service.compile_preview("sample", "alice")
    prow = db.fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
    if prow is None:
        raise RuntimeError("preview record missing")

    build_id = build_service.run_build("sample", "alice")
    brow = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id])
    if brow is None or brow["status"] != "ok":
        raise RuntimeError(f"build failed: {brow}")

    run_id_ws = run_service.run_submission("sample", "alice", build_id, submission_path="solutions/main.cpp", mode="pass-fail")
    rrow_ws = db.fetch_one("SELECT status FROM runs WHERE id=?", [run_id_ws])
    if rrow_ws is None or rrow_ws["status"] != "ok":
        raise RuntimeError(f"workspace run failed: {rrow_ws}")

    upload_src = (
        b'#include <bits/stdc++.h>\nusing namespace std; int main(){ long long x; if(!(cin>>x)) return 0; cout<<x<<"\\n"; }\n'
    )
    run_id_upload = run_service.run_submission(
        "sample",
        "alice",
        build_id,
        mode="pass-fail",
        upload_content=upload_src,
        upload_filename="upload.cpp",
    )
    rrow_upload = db.fetch_one("SELECT status FROM runs WHERE id=?", [run_id_upload])
    if rrow_upload is None or rrow_upload["status"] != "ok":
        raise RuntimeError(f"upload run failed: {rrow_upload}")

    run_id_multi = run_service.run_submission("sample", "alice", build_id, submission_path="solutions/main.cpp", mode="multi-pass")
    rrow_multi = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_multi])
    if rrow_multi is None or rrow_multi["status"] != "ok":
        raise RuntimeError(f"multi-pass run failed: {rrow_multi}")
    multi_summary = json.loads(rrow_multi["summary_json"])
    if not multi_summary.get("tests") or len(multi_summary["tests"][0].get("passes", [])) < 2:
        raise RuntimeError("multi-pass run did not execute multiple passes")

    run_id_interactive = run_service.run_submission("sample", "alice", build_id, submission_path="solutions/main.cpp", mode="interactive")
    rrow_interactive = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_interactive])
    if rrow_interactive is None or rrow_interactive["status"] != "ok":
        raise RuntimeError(f"interactive run failed: {rrow_interactive}")
    interactive_summary = json.loads(rrow_interactive["summary_json"])
    if not interactive_summary.get("tests") or interactive_summary["tests"][0].get("verdict") != "OK":
        raise RuntimeError("interactive run did not produce OK verdict")

    run_id_missing = run_service.run_submission(
        "sample",
        "alice",
        build_id,
        submission_path="solutions/missing.cpp",
        mode="pass-fail",
    )
    rrow_missing = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_missing])
    if rrow_missing is None or rrow_missing["status"] != "failed":
        raise RuntimeError(f"missing-source run should fail: {rrow_missing}")
    missing_summary = json.loads(rrow_missing["summary_json"])
    if missing_summary.get("compile_log") != "compile.log":
        raise RuntimeError("missing-source failure did not expose compile.log")

    export_outputs: dict[str, Path] = {}
    for export_type in ["kattis", "domjudge", "polygon-standard", "polygon-full"]:
        out = export_service.create_export("sample", build_id, export_type)
        if not out.exists():
            raise RuntimeError(f"missing export {export_type}")
        export_outputs[export_type] = out

    kattis_entries = _zip_entries(export_outputs["kattis"])
    _expect_suffix(kattis_entries, "problem.yaml", "kattis")
    _expect_suffix(kattis_entries, "statement/problem.en.tex", "kattis")
    _expect_suffix(kattis_entries, "data/secret/001.in", "kattis")
    _expect_suffix(kattis_entries, "submissions/accepted/accepted.cpp", "kattis")
    _expect_suffix(kattis_entries, "input_validators/validator.cpp", "kattis")

    domjudge_entries = _zip_entries(export_outputs["domjudge"])
    _expect_suffix(domjudge_entries, "problem.yaml", "domjudge")
    _expect_suffix(domjudge_entries, "problem_statement/problem.en.tex", "domjudge")
    _expect_suffix(domjudge_entries, "data/secret/001.in", "domjudge")
    _expect_suffix(domjudge_entries, "submissions/accepted/accepted.cpp", "domjudge")
    _expect_suffix(domjudge_entries, "input_validators/validator.cpp", "domjudge")

    polygon_standard_entries = _zip_entries(export_outputs["polygon-standard"])
    _expect_suffix(polygon_standard_entries, "manifest.json", "polygon-standard")
    _expect_absent_fragment(polygon_standard_entries, "/logs/run-", "polygon-standard")
    if _has_suffix(polygon_standard_entries, "tests/001.in"):
        raise RuntimeError("polygon-standard should not contain tests/")

    polygon_full_entries = _zip_entries(export_outputs["polygon-full"])
    _expect_suffix(polygon_full_entries, "manifest.json", "polygon-full")
    _expect_suffix(polygon_full_entries, "tests/001.in", "polygon-full")
    _expect_suffix(polygon_full_entries, "ans/001.ans", "polygon-full")
    _expect_absent_fragment(polygon_full_entries, "/logs/run-", "polygon-full")

    with TestClient(app) as client:
        r = client.get(
            f"/problems/sample/alice/artifacts/{build_id}/download-dir",
            params={"rel": "tests"},
        )
        if r.status_code != 200 or r.headers.get("content-type", "").find("zip") == -1:
            raise RuntimeError(f"download-dir failed status={r.status_code}")

    print("smoke_ok", preview_id, build_id, run_id_ws, run_id_upload, run_id_multi, run_id_interactive)


if __name__ == "__main__":
    main()
