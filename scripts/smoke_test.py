#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
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

    (ws / "config/build.json").write_text(
        json.dumps(
            {
                "generator_runs": 1,
                "compile_jobs": 3,
                "run_jobs": 2,
                "validator_args": ["--self-check"],
                "checker_mode": "testlib",
                "checker_args": [],
                "max_passes": 8,
                "accepted_source": "solutions/accepted.cc",
            }
        ),
        encoding="utf-8",
    )
    (ws / "tests/manual/001.in").write_text("1\n", encoding="utf-8")
    (ws / "tests/manual/001.ans").write_text("ignored sidecar answer\n", encoding="utf-8")
    (ws / "solutions/accepted.cpp").write_text(
        '#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 0; cout<<x<<"\\n";}',
        encoding="utf-8",
    )
    (ws / "solutions/main.cpp").write_text(
        '#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 0; cout<<x<<"\\n";}',
        encoding="utf-8",
    )
    (ws / "solutions/accepted.cc").write_text(
        '#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 0; cout<<x<<"\\n";}',
        encoding="utf-8",
    )
    (ws / "validators/validator.cpp").write_text(
        "#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 43; if(x<0) return 43; string rest; getline(cin,rest); return 42;}",
        encoding="utf-8",
    )
    (ws / "checkers/checker.cpp").write_text(
        "#include <bits/stdc++.h>\n#include <filesystem>\nusing namespace std; int main(int argc,char** argv){ifstream out(argv[2]); long long a=0; if(!(out>>a)) return 1; const char* fb=getenv(\"FEEDBACK_DIR\"); if(fb){ filesystem::create_directories(fb); ofstream(string(fb)+\"/judgemessage.txt\")<<\"judge\"; ofstream(string(fb)+\"/teammessage.txt\")<<\"team\"; if(a>0){ ofstream(string(fb)+\"/nextpass.in\")<<(a-1)<<\"\\n\"; } } return a>=0?42:43; }",
        encoding="utf-8",
    )
    (ws / "interactors/interactor.cpp").write_text(
        "#include <bits/stdc++.h>\n#include <filesystem>\nusing namespace std; int main(int argc,char** argv){ if(argc<2) return 1; ifstream in(argv[1]); long long x=0; if(!(in>>x)) return 1; cout<<x<<endl; long long y=0; if(!(cin>>y)) return 1; const char* fb=getenv(\"FEEDBACK_DIR\"); if(fb){ filesystem::create_directories(fb); ofstream(string(fb)+\"/judgemessage.txt\")<<\"judge\"; ofstream(string(fb)+\"/teammessage.txt\")<<\"team\"; } return x==y?42:43; }",
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
    manifest = json.loads((Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "manifest.json").read_text(encoding="utf-8"))
    generation_params = manifest.get("generation_params", {})
    if int(generation_params.get("max_passes", 0)) != 8:
        raise RuntimeError(f"manifest generation_params missing max_passes: {generation_params}")
    if int(generation_params.get("compile_jobs", 0)) != 3:
        raise RuntimeError(f"manifest generation_params missing compile_jobs: {generation_params}")
    if int(generation_params.get("run_jobs", 0)) != 2:
        raise RuntimeError(f"manifest generation_params missing run_jobs: {generation_params}")
    if int(manifest.get("summary", {}).get("tests_count", -1)) != 2:
        raise RuntimeError("manual sidecar files should not be treated as test inputs")
    compile_log = (Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "logs" / "compile.log").read_text(encoding="utf-8")
    if "compile_jobs=3" not in compile_log:
        raise RuntimeError("compile log missing configured compile_jobs marker")
    cache_root = Path(os.environ["POLYGONLIKE_CACHE_ROOT"]) / "compile"
    cache_count = lambda: len(list(cache_root.rglob("*.bin")))
    cache_after_build_first = cache_count()
    build_id_repeat = build_service.run_build("sample", "alice")
    brow_repeat = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id_repeat])
    if brow_repeat is None or brow_repeat["status"] != "ok":
        raise RuntimeError(f"repeat build failed: {brow_repeat}")
    cache_after_build_repeat = cache_count()
    if cache_after_build_repeat != cache_after_build_first:
        raise RuntimeError("compile cache did not reuse unchanged build targets")

    run_id_ws = run_service.run_submission("sample", "alice", build_id, submission_path="solutions/main.cpp", mode="pass-fail")
    rrow_ws = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_ws])
    if rrow_ws is None or rrow_ws["status"] != "ok":
        raise RuntimeError(f"workspace run failed: {rrow_ws}")
    ws_summary = json.loads(rrow_ws["summary_json"])
    if int(ws_summary.get("run_config", {}).get("run_jobs", 0)) != 2:
        raise RuntimeError("run config did not preserve run_jobs=2")
    if int(ws_summary.get("run_config", {}).get("run_jobs_effective", 0)) != 2:
        raise RuntimeError("run config did not expose expected effective run_jobs")

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
    if multi_summary.get("run_config", {}).get("checker_mode") != "testlib":
        raise RuntimeError("run config did not preserve checker_mode=testlib")
    if int(multi_summary.get("run_config", {}).get("run_jobs", 0)) != 2:
        raise RuntimeError("multi-pass run config did not preserve run_jobs=2")
    if int(multi_summary.get("run_config", {}).get("run_jobs_effective", 0)) != 2:
        raise RuntimeError("multi-pass run did not expose expected effective run_jobs")
    if not multi_summary.get("tests") or len(multi_summary["tests"][0].get("passes", [])) < 2:
        raise RuntimeError("multi-pass run did not execute multiple passes")

    run_id_interactive = run_service.run_submission("sample", "alice", build_id, submission_path="solutions/main.cpp", mode="interactive")
    rrow_interactive = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_interactive])
    if rrow_interactive is None or rrow_interactive["status"] != "ok":
        raise RuntimeError(f"interactive run failed: {rrow_interactive}")
    interactive_summary = json.loads(rrow_interactive["summary_json"])
    if not interactive_summary.get("tests") or interactive_summary["tests"][0].get("verdict") != "OK":
        raise RuntimeError("interactive run did not produce OK verdict")

    dep_stem = f"cache_dep_{uuid.uuid4().hex[:8]}"
    dep_header = ws / f"solutions/{dep_stem}.h"
    dep_source = ws / f"solutions/{dep_stem}.cpp"
    dep_header.write_text("#define ANSWER_VALUE 1\n", encoding="utf-8")
    dep_source.write_text(
        f'#include "{dep_stem}.h"\n#include <bits/stdc++.h>\nusing namespace std; int main(){{ cout<<ANSWER_VALUE<<"\\n"; }}\n',
        encoding="utf-8",
    )
    cache_before = cache_count()
    run_id_cache_first = run_service.run_submission("sample", "alice", build_id, submission_path=f"solutions/{dep_stem}.cpp", mode="pass-fail")
    rrow_cache_first = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_cache_first])
    if rrow_cache_first is None or rrow_cache_first["status"] != "ok":
        raise RuntimeError(f"cache dependency first run failed: {rrow_cache_first}")
    cache_first_summary = json.loads(rrow_cache_first["summary_json"])
    if cache_first_summary["tests"][0]["verdict"] != "OK":
        raise RuntimeError("cache dependency first run should be OK")
    cache_after_first = cache_count()

    run_id_cache_repeat = run_service.run_submission("sample", "alice", build_id, submission_path=f"solutions/{dep_stem}.cpp", mode="pass-fail")
    rrow_cache_repeat = db.fetch_one("SELECT status FROM runs WHERE id=?", [run_id_cache_repeat])
    if rrow_cache_repeat is None or rrow_cache_repeat["status"] != "ok":
        raise RuntimeError(f"cache dependency repeat run failed: {rrow_cache_repeat}")
    cache_after_repeat = cache_count()
    if cache_after_repeat != cache_after_first:
        raise RuntimeError("compile cache did not reuse unchanged source build")

    dep_header.write_text("#define ANSWER_VALUE -1\n", encoding="utf-8")
    run_id_cache_second = run_service.run_submission("sample", "alice", build_id, submission_path=f"solutions/{dep_stem}.cpp", mode="pass-fail")
    rrow_cache_second = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_cache_second])
    if rrow_cache_second is None or rrow_cache_second["status"] != "ok":
        raise RuntimeError(f"cache dependency second run failed: {rrow_cache_second}")
    cache_second_summary = json.loads(rrow_cache_second["summary_json"])
    if cache_second_summary["tests"][0]["verdict"] == "OK":
        raise RuntimeError("compile cache did not invalidate after header dependency change")
    cache_after_second = cache_count()
    if cache_after_second <= cache_after_repeat or cache_after_first < cache_before:
        raise RuntimeError("compile cache counters were inconsistent during dependency checks")

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

    run_id_traversal = run_service.run_submission(
        "sample",
        "alice",
        build_id,
        submission_path="../outside.cpp",
        mode="pass-fail",
    )
    rrow_traversal = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_traversal])
    if rrow_traversal is None or rrow_traversal["status"] != "failed":
        raise RuntimeError(f"path traversal run should fail: {rrow_traversal}")
    traversal_summary = json.loads(rrow_traversal["summary_json"])
    if "workspace" not in str(traversal_summary.get("error", "")):
        raise RuntimeError("path traversal failure did not include workspace boundary error")

    run_id_bad_build = run_service.run_submission(
        "sample",
        "alice",
        "b-does-not-exist",
        submission_path="solutions/main.cpp",
        mode="pass-fail",
    )
    rrow_bad_build = db.fetch_one("SELECT status,summary_json,artifact_path FROM runs WHERE id=?", [run_id_bad_build])
    if rrow_bad_build is None or rrow_bad_build["status"] != "failed":
        raise RuntimeError(f"invalid build run should fail: {rrow_bad_build}")
    bad_build_summary = json.loads(rrow_bad_build["summary_json"])
    if "not runnable" not in str(bad_build_summary.get("error", "")):
        raise RuntimeError("invalid build run did not report preflight failure")
    invalid_runs_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / "invalid-runs"
    if invalid_runs_root.resolve() not in Path(rrow_bad_build["artifact_path"]).resolve().parents:
        raise RuntimeError("invalid build run was not isolated under run_root/invalid-runs")

    build_id_missing_artifacts = build_service.run_build("sample", "alice")
    brow_missing_artifacts = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id_missing_artifacts])
    if brow_missing_artifacts is None or brow_missing_artifacts["status"] != "ok":
        raise RuntimeError(f"missing-artifacts build failed unexpectedly: {brow_missing_artifacts}")
    missing_artifacts_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id_missing_artifacts
    shutil.rmtree(missing_artifacts_root, ignore_errors=True)
    run_id_missing_artifacts = run_service.run_submission(
        "sample",
        "alice",
        build_id_missing_artifacts,
        submission_path="solutions/main.cpp",
        mode="pass-fail",
    )
    rrow_missing_artifacts = db.fetch_one("SELECT status,summary_json,artifact_path FROM runs WHERE id=?", [run_id_missing_artifacts])
    if rrow_missing_artifacts is None or rrow_missing_artifacts["status"] != "failed":
        raise RuntimeError(f"missing-artifacts run should fail: {rrow_missing_artifacts}")
    missing_artifacts_summary = json.loads(rrow_missing_artifacts["summary_json"])
    if "not runnable" not in str(missing_artifacts_summary.get("error", "")):
        raise RuntimeError("missing-artifacts run did not report preflight failure")
    if invalid_runs_root.resolve() not in Path(rrow_missing_artifacts["artifact_path"]).resolve().parents:
        raise RuntimeError("missing-artifacts run was not isolated under run_root/invalid-runs")

    (ws / "config/build.json").write_text(
        json.dumps(
            {
                "generator_runs": 1,
                "validator_args": [],
                "checker_mode": "kattis",
                "checker_args": [],
                "max_passes": 6,
            }
        ),
        encoding="utf-8",
    )
    (ws / "checkers/checker.cpp").write_text(
        "#include <bits/stdc++.h>\n#include <filesystem>\nusing namespace std; int main(int argc,char** argv){ if(argc<4) return 1; ifstream ans(argv[2]); long long expected=0; if(!(ans>>expected)) return 1; long long got=0; if(!(cin>>got)) return 43; filesystem::path fb(argv[3]); filesystem::create_directories(fb); ofstream(fb/\"judgemessage.txt\")<<\"judge\"; ofstream(fb/\"teammessage.txt\")<<\"team\"; if(got>0){ ofstream(fb/\"nextpass.in\")<<(got-1)<<\"\\n\"; } return got==expected?42:43; }",
        encoding="utf-8",
    )
    build_id_kattis = build_service.run_build("sample", "alice")
    brow_kattis = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id_kattis])
    if brow_kattis is None or brow_kattis["status"] != "ok":
        raise RuntimeError(f"kattis checker build failed: {brow_kattis}")
    run_id_kattis = run_service.run_submission("sample", "alice", build_id_kattis, submission_path="solutions/main.cpp", mode="multi-pass")
    rrow_kattis = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_kattis])
    if rrow_kattis is None or rrow_kattis["status"] != "ok":
        raise RuntimeError(f"kattis checker run failed: {rrow_kattis}")
    kattis_summary = json.loads(rrow_kattis["summary_json"])
    if kattis_summary.get("run_config", {}).get("checker_mode") != "kattis":
        raise RuntimeError("run config did not preserve checker_mode=kattis")
    if not kattis_summary.get("tests") or len(kattis_summary["tests"][0].get("passes", [])) < 2:
        raise RuntimeError("kattis checker multi-pass run did not execute multiple passes")

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

    print("smoke_ok", preview_id, build_id, build_id_repeat, build_id_kattis, run_id_ws, run_id_upload, run_id_multi, run_id_interactive, run_id_kattis)


if __name__ == "__main__":
    main()
