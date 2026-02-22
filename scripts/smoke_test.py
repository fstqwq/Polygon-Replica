#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def ensure_local_env() -> None:
    os.environ.setdefault("POLYGONLIKE_DB", "./var/polygonlike.db")
    os.environ.setdefault("POLYGONLIKE_BARE_ROOT", "./var/srv/git")
    os.environ.setdefault("POLYGONLIKE_WORKSPACE_ROOT", "./var/srv/workspaces")
    os.environ.setdefault("POLYGONLIKE_RUN_ROOT", "./var/srv/runs")
    os.environ.setdefault("POLYGONLIKE_ARTIFACTS_ROOT", "./var/lib/polygonlike/artifacts")
    os.environ.setdefault("POLYGONLIKE_CACHE_ROOT", "./var/cache/polygonlike")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    ensure_local_env()

    from app.main import build_service, db, export_service, preview_service, run_service, workspace_service

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
        "#include <bits/stdc++.h>\nusing namespace std; int main(int argc,char** argv){ifstream out(argv[2]), ans(argv[3]); long long a,b; if(!(out>>a) || !(ans>>b)) return 1; return a==b?0:1;}",
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

    for export_type in ["kattis", "domjudge", "polygon-standard", "polygon-full"]:
        out = export_service.create_export("sample", build_id, export_type)
        if not out.exists():
            raise RuntimeError(f"missing export {export_type}")

    print("smoke_ok", preview_id, build_id, run_id_ws, run_id_upload)


if __name__ == "__main__":
    main()
