#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import shutil
import sys
import uuid
import zipfile
from io import BytesIO
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
    from app.services.util import run_cmd

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
        selector_problem = f"buildcap-{uuid.uuid4().hex[:8]}"
        selector_user = f"u-{uuid.uuid4().hex[:6]}"
        workspace_service.ensure_problem(selector_problem, "Build Selector Cap Problem")
        workspace_service.ensure_workspace(selector_problem, selector_user)
        selector_ctx = workspace_service.workspace_context(selector_problem, selector_user)
        selector_prefix = f"b-listcap-{uuid.uuid4().hex[:6]}"
        selector_count = 260
        selector_limit = 200
        for i in range(selector_count):
            build_id = f"{selector_prefix}-{i:03d}"
            created = f"2100-01-01T00:{i // 60:02d}:{i % 60:02d}Z"
            artifact_path = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / selector_problem / build_id
            db.execute(
                "INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    build_id,
                    selector_ctx["problem"]["id"],
                    selector_ctx["workspace"]["id"],
                    "feedfacefeedfacefeedfacefeedfacefeedface",
                    "main",
                    "ok",
                    "{}",
                    str(artifact_path),
                    created,
                    created,
                ],
            )
        run_list_page = client.get(f"/problems/{selector_problem}/{selector_user}/run")
        if run_list_page.status_code != 200:
            raise RuntimeError(f"run page failed during build selector limit check: {run_list_page.status_code}")
        run_html = run_list_page.text
        if run_html.count(f'value="{selector_prefix}-') != selector_limit:
            raise RuntimeError("run page build selector did not enforce expected build-row limit")
        if f'value="{selector_prefix}-259"' not in run_html:
            raise RuntimeError("run page build selector omitted newest capped build id")
        if f'value="{selector_prefix}-000"' in run_html:
            raise RuntimeError("run page build selector included oldest build id beyond cap")
        export_list_page = client.get(f"/problems/{selector_problem}/{selector_user}/export")
        if export_list_page.status_code != 200:
            raise RuntimeError(f"export page failed during build selector limit check: {export_list_page.status_code}")
        export_html = export_list_page.text
        if export_html.count(f'value="{selector_prefix}-') != selector_limit:
            raise RuntimeError("export page build selector did not enforce expected build-row limit")
        if f'value="{selector_prefix}-259"' not in export_html:
            raise RuntimeError("export page build selector omitted newest capped build id")
        if f'value="{selector_prefix}-000"' in export_html:
            raise RuntimeError("export page build selector included oldest build id beyond cap")
        invalid_problem_switch = client.post(
            "/switch-workspace",
            data={"problem": "..", "user": "alice", "page": "files"},
            follow_redirects=False,
        )
        if invalid_problem_switch.status_code != 400:
            raise RuntimeError(
                "switch-workspace should reject invalid problem identifiers"
                f", status={invalid_problem_switch.status_code}"
            )
        invalid_user_switch = client.post(
            "/switch-workspace",
            data={"problem": "sample", "user": "..", "page": "files"},
            follow_redirects=False,
        )
        if invalid_user_switch.status_code != 400:
            raise RuntimeError(
                "switch-workspace should reject invalid user identifiers"
                f", status={invalid_user_switch.status_code}"
            )
        invalid_problem_status = client.get("/api/problems/%2E%2E/workspaces/alice/status")
        if invalid_problem_status.status_code != 400:
            raise RuntimeError(
                "workspace status API should reject invalid problem identifiers"
                f", status={invalid_problem_status.status_code}"
            )
        invalid_user_status = client.get("/api/problems/sample/workspaces/%2E%2E/status")
        if invalid_user_status.status_code != 400:
            raise RuntimeError(
                "workspace status API should reject invalid user identifiers"
                f", status={invalid_user_status.status_code}"
            )
        invalid_problem_build_run = client.post(
            "/problems/%2E%2E/alice/build/run",
            data={"commit": ""},
            follow_redirects=False,
        )
        if invalid_problem_build_run.status_code != 400:
            raise RuntimeError(
                "build run route should reject invalid problem identifiers"
                f", status={invalid_problem_build_run.status_code}"
            )
        invalid_problem_preview_run = client.post(
            "/problems/%2E%2E/alice/preview/run",
            data={"commit": ""},
            follow_redirects=False,
        )
        if invalid_problem_preview_run.status_code != 400:
            raise RuntimeError(
                "preview run route should reject invalid problem identifiers"
                f", status={invalid_problem_preview_run.status_code}"
            )
        invalid_problem_run_execute = client.post(
            "/problems/%2E%2E/alice/run/execute",
            data={"build_id": "b-missing", "mode": "pass-fail", "submission_path": "solutions/main.cpp"},
            follow_redirects=False,
        )
        if invalid_problem_run_execute.status_code != 400:
            raise RuntimeError(
                "run execute route should reject invalid problem identifiers"
                f", status={invalid_problem_run_execute.status_code}"
            )
        lazy_user = f"lazyctx-{uuid.uuid4().hex[:8]}"
        lazy_user_files = client.get(f"/problems/sample/{lazy_user}/files")
        if lazy_user_files.status_code != 200:
            raise RuntimeError(
                "files page should lazily provision unknown-but-valid users"
                f", status={lazy_user_files.status_code}"
            )
        race_user = f"wsrace-{uuid.uuid4().hex[:8]}"
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(workspace_service.ensure_workspace, "sample", race_user, False)
                for _ in range(4)
            ]
            resolved_paths = {str(Path(future.result()).resolve()) for future in as_completed(futures)}
        problem_row = db.fetch_one("SELECT id FROM problems WHERE slug=?", ["sample"])
        user_row = db.fetch_one("SELECT id FROM users WHERE username=?", [race_user])
        if problem_row is None or user_row is None:
            raise RuntimeError("workspace provision race check setup failed")
        race_rows = db.fetch_all(
            "SELECT path FROM workspaces WHERE problem_id=? AND user_id=?",
            [problem_row["id"], user_row["id"]],
        )
        if len(race_rows) != 1:
            raise RuntimeError(f"workspace provision race should create exactly one row, got={len(race_rows)}")
        race_workspace = Path(str(race_rows[0]["path"])).resolve()
        if str(race_workspace) not in resolved_paths:
            raise RuntimeError("workspace provision race returned inconsistent workspace paths")
        if not (race_workspace / ".git").is_dir():
            raise RuntimeError("workspace provision race produced workspace without git metadata")
        bad_line = client.get(
            "/problems/sample/alice/files",
            params={"path": "README.problem.md", "line": "not-a-number"},
        )
        if bad_line.status_code != 200:
            raise RuntimeError(f"files page should tolerate invalid line query, status={bad_line.status_code}")
        legacy_manifest = client.get("/api/problems/sample/builds/b-nonexistent/manifest")
        if legacy_manifest.status_code != 400:
            raise RuntimeError(f"legacy manifest endpoint should require workspace context, status={legacy_manifest.status_code}")
        for posted_page, expected_page in [("runs", "run"), ("artifacts", "build"), ("not-a-page", "files")]:
            switch_workspace = client.post(
                "/switch-workspace",
                data={"problem": "sample", "user": "alice", "page": posted_page},
                follow_redirects=False,
            )
            if switch_workspace.status_code != 303:
                raise RuntimeError(
                    f"switch-workspace should redirect for page={posted_page}, status={switch_workspace.status_code}"
                )
            location = switch_workspace.headers.get("location", "")
            if location != f"/problems/sample/alice/{expected_page}":
                raise RuntimeError(
                    f"switch-workspace page normalization mismatch: page={posted_page} location={location}"
                )
        for posted_page, expected_page in [("runs", "run"), ("artifacts", "build"), ("not-a-page", "files")]:
            switch_branch = client.post(
                "/switch-branch",
                data={"problem": "sample", "user": "alice", "branch": "main", "page": posted_page},
                follow_redirects=False,
            )
            if switch_branch.status_code != 303:
                raise RuntimeError(f"switch-branch should redirect for page={posted_page}, status={switch_branch.status_code}")
            location = switch_branch.headers.get("location", "")
            if location != f"/problems/sample/alice/{expected_page}":
                raise RuntimeError(f"switch-branch page normalization mismatch: page={posted_page} location={location}")
        upload_path = f"notes/upload-smoke-{uuid.uuid4().hex[:8]}.bin"
        upload_payload = bytes(range(256)) * 4096
        upload_resp = client.post(
            "/problems/sample/alice/files/upload",
            data={"path": upload_path},
            files={"upload": ("upload-smoke.bin", upload_payload, "application/octet-stream")},
            follow_redirects=False,
        )
        if upload_resp.status_code != 303:
            raise RuntimeError(f"files upload should redirect status={upload_resp.status_code}")
        download_resp = client.get(
            "/problems/sample/alice/files/download",
            params={"path": upload_path},
        )
        if download_resp.status_code != 200:
            raise RuntimeError(f"uploaded file download failed status={download_resp.status_code}")
        if download_resp.content != upload_payload:
            raise RuntimeError("uploaded file payload mismatch after round-trip")
        forbidden_download = client.get(
            "/problems/sample/alice/files/download",
            params={"path": ".git/config"},
        )
        if forbidden_download.status_code != 400:
            raise RuntimeError(f"reserved-path download should be rejected status={forbidden_download.status_code}")
        root_download = client.get(
            "/problems/sample/alice/files/download",
            params={"path": "."},
        )
        if root_download.status_code != 400:
            raise RuntimeError(f"workspace-root download should be rejected status={root_download.status_code}")
        forbidden_upload = client.post(
            "/problems/sample/alice/files/upload",
            data={"path": ".git/forbidden.bin"},
            files={"upload": ("forbidden.bin", b"x", "application/octet-stream")},
            follow_redirects=False,
        )
        if forbidden_upload.status_code != 400:
            raise RuntimeError(f"reserved-path upload should be rejected status={forbidden_upload.status_code}")
        dir_upload = client.post(
            "/problems/sample/alice/files/upload",
            data={"path": "tests"},
            files={"upload": ("forbidden.bin", b"x", "application/octet-stream")},
            follow_redirects=False,
        )
        if dir_upload.status_code != 400:
            raise RuntimeError(f"directory-target upload should be rejected status={dir_upload.status_code}")
        forbidden_save = client.post(
            "/problems/sample/alice/files/save",
            data={"path": ".git/config", "content": "x"},
            follow_redirects=False,
        )
        if forbidden_save.status_code != 303 or "reserved+path" not in forbidden_save.headers.get("location", ""):
            raise RuntimeError(f"reserved-path save should redirect with error message: {forbidden_save.headers}")
        dir_save = client.post(
            "/problems/sample/alice/files/save",
            data={"path": "tests", "content": "x"},
            follow_redirects=False,
        )
        if dir_save.status_code != 303 or "path+is+a+directory" not in dir_save.headers.get("location", ""):
            raise RuntimeError(f"directory-target save should redirect with error message: {dir_save.headers}")
        forbidden_new = client.post(
            "/problems/sample/alice/files/new",
            data={"path": ".git/new.txt"},
            follow_redirects=False,
        )
        if forbidden_new.status_code != 303 or "reserved+path" not in forbidden_new.headers.get("location", ""):
            raise RuntimeError(f"reserved-path new should redirect with error message: {forbidden_new.headers}")
        dir_new = client.post(
            "/problems/sample/alice/files/new",
            data={"path": "tests"},
            follow_redirects=False,
        )
        if dir_new.status_code != 303 or "path+is+a+directory" not in dir_new.headers.get("location", ""):
            raise RuntimeError(f"directory-target new should redirect with error message: {dir_new.headers}")
        forbidden_rename = client.post(
            "/problems/sample/alice/files/rename",
            data={"old_path": "README.problem.md", "new_path": ".git/renamed.txt"},
            follow_redirects=False,
        )
        if forbidden_rename.status_code != 303 or "reserved+path" not in forbidden_rename.headers.get("location", ""):
            raise RuntimeError(f"reserved-path rename should redirect with error message: {forbidden_rename.headers}")
        missing_rename = client.post(
            "/problems/sample/alice/files/rename",
            data={"old_path": "no/such/file.txt", "new_path": "tmp/new.txt"},
            follow_redirects=False,
        )
        if missing_rename.status_code != 303 or "path+not+found" not in missing_rename.headers.get("location", ""):
            raise RuntimeError(f"missing-source rename should redirect with error message: {missing_rename.headers}")
        forbidden_delete = client.post(
            "/problems/sample/alice/files/delete",
            data={"path": ".git/config"},
            follow_redirects=False,
        )
        if forbidden_delete.status_code != 303 or "reserved+path" not in forbidden_delete.headers.get("location", ""):
            raise RuntimeError(f"reserved-path delete should redirect with error message: {forbidden_delete.headers}")

    snapshot_repo = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"snapshot-check-{uuid.uuid4().hex[:8]}"
    snapshot_repo.mkdir(parents=True, exist_ok=True)
    snapshot_symlink_source = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"snapshot-link-source-{uuid.uuid4().hex[:8]}.txt"
    snapshot_symlink_source.write_text("snapshot-link-source\n", encoding="utf-8")
    snapshot_symlink_supported = True
    try:
        (snapshot_repo / "tracked-link.txt").symlink_to(snapshot_symlink_source)
    except (OSError, NotImplementedError):
        snapshot_symlink_supported = False
    for cmd in [
        ["git", "init", str(snapshot_repo)],
        ["git", "-C", str(snapshot_repo), "config", "user.email", "smoke@polygonlike.local"],
        ["git", "-C", str(snapshot_repo), "config", "user.name", "Smoke Test"],
    ]:
        proc = run_cmd(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"snapshot setup command failed: {' '.join(cmd)}")
    (snapshot_repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    for cmd in [
        ["git", "-C", str(snapshot_repo), "add", "."],
        ["git", "-C", str(snapshot_repo), "commit", "-m", "init"],
    ]:
        proc = run_cmd(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"snapshot setup command failed: {' '.join(cmd)}")

    clean_snapshot = workspace_service.create_snapshot(snapshot_repo, None)
    if not (clean_snapshot / "tracked.txt").exists():
        raise RuntimeError("clean snapshot path did not preserve tracked files")
    if (clean_snapshot / "untracked.txt").exists():
        raise RuntimeError("clean snapshot unexpectedly contained untracked files")
    if snapshot_symlink_supported and (clean_snapshot / "tracked-link.txt").exists():
        raise RuntimeError("clean snapshot should strip committed symlinks")
    shutil.rmtree(clean_snapshot.parent, ignore_errors=True)

    (snapshot_repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    if snapshot_symlink_supported:
        try:
            (snapshot_repo / "dirty-link.txt").symlink_to(snapshot_symlink_source)
        except (OSError, NotImplementedError):
            snapshot_symlink_supported = False
    dirty_snapshot = workspace_service.create_snapshot(snapshot_repo, None)
    if not (dirty_snapshot / "untracked.txt").exists():
        raise RuntimeError("dirty snapshot path did not preserve untracked files")
    if snapshot_symlink_supported and (dirty_snapshot / "dirty-link.txt").exists():
        raise RuntimeError("dirty snapshot should strip workspace symlinks")
    shutil.rmtree(dirty_snapshot.parent, ignore_errors=True)
    shutil.rmtree(snapshot_repo, ignore_errors=True)
    snapshot_symlink_source.unlink(missing_ok=True)

    ctx = workspace_service.workspace_context("sample", "alice")
    ws = Path(ctx["workspace"]["path"])
    head_commit = str(ctx["workspace"].get("head_commit") or "").strip()
    if not head_commit:
        head_commit = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
    alice_user = db.fetch_one("SELECT id FROM users WHERE username='alice'")
    if alice_user is None:
        raise RuntimeError("alice user row missing")
    expected_workspace_path = (Path(os.environ["POLYGONLIKE_WORKSPACE_ROOT"]) / str(alice_user["id"]) / "sample").resolve()
    db.execute(
        "UPDATE workspaces SET path=? WHERE problem_id=? AND user_id=?",
        [str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]).resolve()), ctx["problem"]["id"], alice_user["id"]],
    )
    try:
        with TestClient(app) as client:
            tampered_ws = client.get("/problems/sample/alice/files")
            if tampered_ws.status_code == 200:
                raise RuntimeError("workspace path mismatch should not allow normal page rendering")
            if "workspace path mismatch" not in tampered_ws.text:
                raise RuntimeError("workspace path mismatch was not surfaced in response body")
    finally:
        db.execute(
            "UPDATE workspaces SET path=? WHERE problem_id=? AND user_id=?",
            [str(expected_workspace_path), ctx["problem"]["id"], alice_user["id"]],
        )
    ws = expected_workspace_path
    list_leak_dir = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"files-list-leak-dir-{uuid.uuid4().hex[:8]}"
    list_leak_dir.mkdir(parents=True, exist_ok=True)
    list_leak_name = f"workspace-list-leak-{uuid.uuid4().hex[:8]}.txt"
    (list_leak_dir / list_leak_name).write_text("leak\n", encoding="utf-8")
    list_leak_file = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"files-list-leak-file-{uuid.uuid4().hex[:8]}.txt"
    list_leak_file.write_text("leak\n", encoding="utf-8")
    link_dir = ws / f"link-outside-dir-{uuid.uuid4().hex[:8]}"
    link_file = ws / f"link-outside-file-{uuid.uuid4().hex[:8]}.txt"
    list_symlink_supported = True
    try:
        link_dir.symlink_to(list_leak_dir, target_is_directory=True)
        link_file.symlink_to(list_leak_file)
    except (OSError, NotImplementedError):
        list_symlink_supported = False
    if list_symlink_supported:
        try:
            with TestClient(app) as client:
                files_page = client.get("/problems/sample/alice/files")
                if files_page.status_code != 200:
                    raise RuntimeError(f"files page failed during symlink listing hardening check: {files_page.status_code}")
                if list_leak_name in files_page.text:
                    raise RuntimeError("files page leaked symlinked directory contents outside workspace")
                if link_file.name in files_page.text:
                    raise RuntimeError("files page should not list symlinked outside files")
        finally:
            link_dir.unlink(missing_ok=True)
            link_file.unlink(missing_ok=True)
    shutil.rmtree(list_leak_dir, ignore_errors=True)
    list_leak_file.unlink(missing_ok=True)

    cached_preview_id = f"p-{uuid.uuid4().hex[:12]}"
    cached_preview_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / cached_preview_id
    (cached_preview_root / "statement_preview").mkdir(parents=True, exist_ok=True)
    (cached_preview_root / "logs").mkdir(parents=True, exist_ok=True)
    cached_pdf = cached_preview_root / "statement_preview" / "statement.pdf"
    cached_log = cached_preview_root / "logs" / "latex.log"
    cached_pdf.write_bytes(b"%PDF-1.4\n% cached preview\n")
    cached_log.write_text("cached latex log\n", encoding="utf-8")
    db.execute(
        "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            cached_preview_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "ok",
            str(cached_preview_root),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    poisoned_preview_id = f"p-poison-{uuid.uuid4().hex[:8]}"
    poison_preview_marker = f"poison-preview-log-{uuid.uuid4().hex[:8]}"
    poison_preview_pdf = ws / "statement_preview" / "statement.pdf"
    poison_preview_log = ws / "logs" / "latex.log"
    poison_preview_pdf.parent.mkdir(parents=True, exist_ok=True)
    poison_preview_log.parent.mkdir(parents=True, exist_ok=True)
    poison_preview_pdf.write_bytes(b"%PDF-1.4\n% poisoned preview source\n")
    poison_preview_log.write_text(poison_preview_marker + "\n", encoding="utf-8")
    db.execute(
        "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            poisoned_preview_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "ok",
            str(ws),
            "9999-01-01T00:00:00Z",
            "9999-01-01T00:00:00Z",
        ],
    )
    # Insert many poisoned preview candidates so reuse logic must scan beyond a small recent-window limit.
    for i in range(1, 31):
        db.execute(
            "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                f"p-poison-batch-{uuid.uuid4().hex[:8]}",
                ctx["problem"]["id"],
                ctx["workspace"]["id"],
                head_commit,
                ctx["workspace"].get("branch") or "main",
                "ok",
                str(ws),
                f"9999-01-01T00:00:{i:02d}Z",
                f"9999-01-01T00:00:{i:02d}Z",
            ],
        )
    # Insert a same-timestamp candidate set larger than one batch to ensure keyset pagination
    # can continue scanning within equal created_at values.
    tie_prefix = f"p-tie-{uuid.uuid4().hex[:6]}"
    tie_created_at = f"9999-12-31T23:59:59.{uuid.uuid4().hex}Z"
    tie_valid_preview_id = f"{tie_prefix}-000"
    tie_valid_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / tie_valid_preview_id
    (tie_valid_root / "statement_preview").mkdir(parents=True, exist_ok=True)
    (tie_valid_root / "logs").mkdir(parents=True, exist_ok=True)
    (tie_valid_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n% tie-paged preview\n")
    (tie_valid_root / "logs" / "latex.log").write_text("tie-paged latex log\n", encoding="utf-8")
    db.execute(
        "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            tie_valid_preview_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "ok",
            str(tie_valid_root),
            tie_created_at,
            tie_created_at,
        ],
    )
    for i in range(1, 71):
        db.execute(
            "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                f"{tie_prefix}-{i:03d}",
                ctx["problem"]["id"],
                ctx["workspace"]["id"],
                head_commit,
                ctx["workspace"].get("branch") or "main",
                "ok",
                str(ws),
                tie_created_at,
                tie_created_at,
            ],
        )
    traversal_preview_id = ".."
    traversal_preview_marker = f"traversal-preview-log-{uuid.uuid4().hex[:8]}"
    traversal_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"])
    traversal_pdf = traversal_root / "statement_preview" / "statement.pdf"
    traversal_log = traversal_root / "logs" / "latex.log"
    prior_traversal_pdf = traversal_pdf.read_bytes() if traversal_pdf.exists() else None
    prior_traversal_log = traversal_log.read_bytes() if traversal_log.exists() else None
    traversal_pdf.parent.mkdir(parents=True, exist_ok=True)
    traversal_log.parent.mkdir(parents=True, exist_ok=True)
    traversal_pdf.write_bytes(b"%PDF-1.4\n% traversal preview source\n")
    traversal_log.write_text(traversal_preview_marker + "\n", encoding="utf-8")
    db.execute("DELETE FROM previews WHERE id=?", [traversal_preview_id])
    db.execute(
        "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            traversal_preview_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "ok",
            str(traversal_root),
            "9999-01-01T00:00:01Z",
            "9999-01-01T00:00:01Z",
        ],
    )
    commit_ref = str(ctx["workspace"].get("branch") or "main")
    reused_preview_id = preview_service.compile_preview("sample", "alice", commit=commit_ref)
    reused_preview = db.fetch_one("SELECT status,summary_json,source_commit,source_ref FROM previews WHERE id=?", [reused_preview_id])
    if reused_preview is None or reused_preview["status"] != "ok":
        raise RuntimeError(f"commit preview reuse failed: {reused_preview}")
    if reused_preview["source_commit"] != head_commit:
        raise RuntimeError("commit preview did not canonicalize source_commit to SHA")
    if str(reused_preview["source_ref"] or "") != commit_ref:
        raise RuntimeError("commit preview did not preserve source_ref")
    reused_summary = json.loads(reused_preview["summary_json"]) if reused_preview["summary_json"] else {}
    reused_from = reused_summary.get("reused_from")
    if not reused_from:
        raise RuntimeError(f"commit preview did not reuse cached artifact: {reused_summary}")
    if reused_from != tie_valid_preview_id:
        raise RuntimeError(
            "commit preview reuse did not scan across same-timestamp pages to reach a later valid candidate"
        )
    if reused_from == poisoned_preview_id:
        raise RuntimeError("commit preview reuse trusted DB-provided poisoned preview artifact_path")
    if reused_from == traversal_preview_id:
        raise RuntimeError("commit preview reuse accepted traversal-style preview id root")
    source_row = db.fetch_one("SELECT artifact_path FROM previews WHERE id=?", [reused_from])
    if source_row is None:
        raise RuntimeError(f"commit preview reuse source missing: {reused_from}")
    source_root = Path(source_row["artifact_path"])
    reused_preview_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / reused_preview_id
    if (reused_preview_root / "statement_preview" / "statement.pdf").read_bytes() != (source_root / "statement_preview" / "statement.pdf").read_bytes():
        raise RuntimeError("reused preview pdf mismatch")
    if (reused_preview_root / "logs" / "latex.log").read_text(encoding="utf-8") != (source_root / "logs" / "latex.log").read_text(encoding="utf-8"):
        raise RuntimeError("reused preview log mismatch")
    if prior_traversal_pdf is None:
        traversal_pdf.unlink(missing_ok=True)
    else:
        traversal_pdf.write_bytes(prior_traversal_pdf)
    if prior_traversal_log is None:
        traversal_log.unlink(missing_ok=True)
    else:
        traversal_log.write_bytes(prior_traversal_log)

    bad_preview_ref = f"does-not-exist-{uuid.uuid4().hex[:8]}"
    bad_preview_id = preview_service.compile_preview("sample", "alice", commit=bad_preview_ref)
    bad_preview = db.fetch_one("SELECT status,summary_json,source_ref FROM previews WHERE id=?", [bad_preview_id])
    if bad_preview is None or bad_preview["status"] != "failed":
        raise RuntimeError(f"invalid commit preview should fail with persisted row: {bad_preview}")
    if str(bad_preview["source_ref"] or "") != bad_preview_ref:
        raise RuntimeError("invalid commit preview did not preserve requested source_ref")
    bad_preview_summary = json.loads(bad_preview["summary_json"]) if bad_preview["summary_json"] else {}
    if not str(bad_preview_summary.get("error", "")).strip():
        raise RuntimeError("invalid commit preview did not preserve resolve error in summary")

    reuse_problem = f"reuse-{uuid.uuid4().hex[:8]}"
    reuse_user = f"u-{uuid.uuid4().hex[:6]}"
    workspace_service.ensure_problem(reuse_problem, "Preview Reuse Problem")
    workspace_service.ensure_workspace(reuse_problem, reuse_user)
    reuse_ctx = workspace_service.workspace_context(reuse_problem, reuse_user)
    reuse_ws = Path(reuse_ctx["workspace"]["path"])
    reuse_head = run_cmd(["git", "-C", str(reuse_ws), "rev-parse", "HEAD"]).stdout.strip()
    reuse_cached_id = f"p-{uuid.uuid4().hex[:12]}"
    reuse_cached_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / reuse_problem / reuse_cached_id
    (reuse_cached_root / "statement_preview").mkdir(parents=True, exist_ok=True)
    (reuse_cached_root / "logs").mkdir(parents=True, exist_ok=True)
    (reuse_cached_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n% clean head cached preview\n")
    (reuse_cached_root / "logs" / "latex.log").write_text("clean head cached log\n", encoding="utf-8")
    db.execute(
        "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            reuse_cached_id,
            reuse_ctx["problem"]["id"],
            reuse_ctx["workspace"]["id"],
            reuse_head,
            reuse_ctx["workspace"].get("branch") or "main",
            "ok",
            str(reuse_cached_root),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    reused_head_preview_id = preview_service.compile_preview(reuse_problem, reuse_user)
    reused_head_preview = db.fetch_one("SELECT status,summary_json FROM previews WHERE id=?", [reused_head_preview_id])
    if reused_head_preview is None or reused_head_preview["status"] != "ok":
        raise RuntimeError(f"workspace-head preview reuse failed: {reused_head_preview}")
    reused_head_summary = json.loads(reused_head_preview["summary_json"]) if reused_head_preview["summary_json"] else {}
    reused_head_from = reused_head_summary.get("reused_from")
    if not reused_head_from:
        raise RuntimeError(f"workspace-head preview did not reuse cached artifact: {reused_head_summary}")
    head_source_row = db.fetch_one("SELECT artifact_path FROM previews WHERE id=?", [reused_head_from])
    if head_source_row is None:
        raise RuntimeError(f"workspace-head preview reuse source missing: {reused_head_from}")
    head_source_root = Path(head_source_row["artifact_path"])
    reused_head_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / reuse_problem / reused_head_preview_id
    if (reused_head_root / "statement_preview" / "statement.pdf").read_bytes() != (
        head_source_root / "statement_preview" / "statement.pdf"
    ).read_bytes():
        raise RuntimeError("workspace-head reused preview pdf mismatch")
    if (reused_head_root / "logs" / "latex.log").read_text(encoding="utf-8") != (
        head_source_root / "logs" / "latex.log"
    ).read_text(encoding="utf-8"):
        raise RuntimeError("workspace-head reused preview log mismatch")

    build_ref_problem = f"buildref-{uuid.uuid4().hex[:8]}"
    build_ref_user = f"u-{uuid.uuid4().hex[:6]}"
    workspace_service.ensure_problem(build_ref_problem, "Build Ref Canonicalization Problem")
    workspace_service.ensure_workspace(build_ref_problem, build_ref_user)
    build_ref_ctx = workspace_service.workspace_context(build_ref_problem, build_ref_user)
    build_ref_ws = Path(build_ref_ctx["workspace"]["path"])
    for d in ["solutions", "validators", "checkers", "tests/manual", "config"]:
        (build_ref_ws / d).mkdir(parents=True, exist_ok=True)
    (build_ref_ws / "tests/manual/001.in").write_text("7\n", encoding="utf-8")
    (build_ref_ws / "solutions/accepted.cpp").write_text(
        "#include <bits/stdc++.h>\nusing namespace std; int main(){ cout<<cin.rdbuf(); }",
        encoding="utf-8",
    )
    (build_ref_ws / "validators/validator.cpp").write_text(
        "#include <bits/stdc++.h>\nint main(){return 42;}",
        encoding="utf-8",
    )
    (build_ref_ws / "checkers/checker.cpp").write_text(
        "#include <bits/stdc++.h>\nint main(){return 42;}",
        encoding="utf-8",
    )
    (build_ref_ws / "config/build.json").write_text(json.dumps({"generator_runs": 0}), encoding="utf-8")
    for cmd in [
        ["git", "-C", str(build_ref_ws), "add", "."],
        ["git", "-C", str(build_ref_ws), "commit", "-m", "seed build-ref smoke files"],
    ]:
        proc = run_cmd(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"build-ref setup command failed: {' '.join(cmd)}")
    build_ref_branch = run_cmd(["git", "-C", str(build_ref_ws), "branch", "--show-current"]).stdout.strip() or "main"
    build_ref_head = run_cmd(["git", "-C", str(build_ref_ws), "rev-parse", "HEAD"]).stdout.strip()
    build_ref_build_id = build_service.run_build(build_ref_problem, build_ref_user, commit=build_ref_branch)
    build_ref_row = db.fetch_one("SELECT status,source_commit,source_ref FROM builds WHERE id=?", [build_ref_build_id])
    if build_ref_row is None or build_ref_row["status"] != "ok":
        raise RuntimeError(f"build-ref commit build failed: {build_ref_row}")
    if str(build_ref_row["source_commit"] or "") != build_ref_head:
        raise RuntimeError("build commit ref was not canonicalized to SHA")
    if str(build_ref_row["source_ref"] or "") != build_ref_branch:
        raise RuntimeError("build source_ref did not preserve requested ref")
    bad_build_ref = f"does-not-exist-{uuid.uuid4().hex[:8]}"
    bad_build_id = build_service.run_build(build_ref_problem, build_ref_user, commit=bad_build_ref)
    bad_build_row = db.fetch_one("SELECT status,summary_json,source_ref FROM builds WHERE id=?", [bad_build_id])
    if bad_build_row is None or bad_build_row["status"] != "failed":
        raise RuntimeError(f"invalid commit build should fail with persisted row: {bad_build_row}")
    if str(bad_build_row["source_ref"] or "") != bad_build_ref:
        raise RuntimeError("invalid commit build did not preserve requested source_ref")
    bad_build_summary = json.loads(bad_build_row["summary_json"]) if bad_build_row["summary_json"] else {}
    if not str(bad_build_summary.get("error", "")).strip():
        raise RuntimeError("invalid commit build did not preserve resolve error in summary")
    try:
        export_service.create_export(build_ref_problem, bad_build_id, "kattis")
        raise RuntimeError("export should reject failed build status")
    except ValueError as exc:
        if "status=failed" not in str(exc):
            raise RuntimeError(f"failed-build export rejection reason mismatch: {exc}")

    binary_problem = f"binaryio-{uuid.uuid4().hex[:8]}"
    binary_user = f"u-{uuid.uuid4().hex[:6]}"
    workspace_service.ensure_problem(binary_problem, "Binary IO Safety Problem")
    workspace_service.ensure_workspace(binary_problem, binary_user)
    binary_ctx = workspace_service.workspace_context(binary_problem, binary_user)
    binary_ws = Path(binary_ctx["workspace"]["path"])
    for d in ["solutions", "validators", "tests/manual", "config"]:
        (binary_ws / d).mkdir(parents=True, exist_ok=True)
    binary_input = b"\xff\xfe\x00\x10abc\n"
    (binary_ws / "tests/manual/001.in").write_bytes(binary_input)
    (binary_ws / "solutions/accepted.cpp").write_text(
        (
            "#include <iostream>\n"
            "int main(){\n"
            "  std::ios::sync_with_stdio(false);\n"
            "  std::cin.tie(nullptr);\n"
            "  char ch;\n"
            "  while (std::cin.get(ch)) std::cout.put(ch);\n"
            "  return 0;\n"
            "}\n"
        ),
        encoding="utf-8",
    )
    (binary_ws / "validators/validator.cpp").write_text(
        "#include <bits/stdc++.h>\nint main(){return 42;}\n",
        encoding="utf-8",
    )
    (binary_ws / "config/build.json").write_text(
        json.dumps({"generator_runs": 0, "require_checker": False}),
        encoding="utf-8",
    )
    for cmd in [
        ["git", "-C", str(binary_ws), "add", "."],
        ["git", "-C", str(binary_ws), "commit", "-m", "seed binary io smoke files"],
    ]:
        proc = run_cmd(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"binary-io setup command failed: {' '.join(cmd)}")
    binary_build_id = build_service.run_build(binary_problem, binary_user)
    binary_build_row = db.fetch_one("SELECT status FROM builds WHERE id=?", [binary_build_id])
    if binary_build_row is None or binary_build_row["status"] != "ok":
        raise RuntimeError(f"binary-io build failed: {binary_build_row}")
    binary_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / binary_problem / binary_build_id
    if (binary_root / "tests" / "001.in").read_bytes() != binary_input:
        raise RuntimeError("binary-io manual test bytes changed during build ingestion")
    if (binary_root / "ans" / "001.ans").read_bytes() != binary_input:
        raise RuntimeError("binary-io answer bytes changed during solve step")
    binary_run_id = run_service.run_submission(
        binary_problem,
        binary_user,
        binary_build_id,
        submission_path="solutions/accepted.cpp",
        mode="pass-fail",
    )
    binary_run_row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [binary_run_id])
    if binary_run_row is None or binary_run_row["status"] != "ok":
        raise RuntimeError(f"binary-io run failed: {binary_run_row}")
    binary_summary = json.loads(binary_run_row["summary_json"])
    if not binary_summary.get("tests") or binary_summary["tests"][0].get("verdict") != "OK":
        raise RuntimeError("binary-io run verdict should be OK")

    for d in ["solutions", "validators", "checkers", "generators", "tests/manual", "config", "statement"]:
        (ws / d).mkdir(parents=True, exist_ok=True)

    (ws / "config/build.json").write_text(
        json.dumps(
            {
                "generator_runs": 1,
                "compile_jobs": 3,
                "validate_jobs": 2,
                "solve_jobs": 2,
                "run_jobs": 2,
                "run_timeout_sec": 1,
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
    (ws / "solutions/slow.cpp").write_text(
        "#include <bits/stdc++.h>\nusing namespace std; int main(){ for(;;){} }\n",
        encoding="utf-8",
    )
    (ws / "solutions/accepted.cc").write_text(
        '#include <bits/stdc++.h>\nusing namespace std; int main(){long long x; if(!(cin>>x)) return 0; cout<<x<<"\\n";}',
        encoding="utf-8",
    )
    (ws / "solutions/re_interactive.cpp").write_text(
        '#include <bits/stdc++.h>\nint main(){std::cerr<<"interactive forced re\\n"; return 1;}',
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
    preview_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / preview_id
    (preview_root / "logs" / "latex.log").write_bytes(b"preview\xfflog\n")
    build_logs_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "logs"
    (build_logs_root / "invalid-utf8.log").write_bytes(b"\xff\xfebuild-log\n")
    with TestClient(app) as client:
        build_page_with_bad_log = client.get("/problems/sample/alice/build", params={"build_id": build_id})
        if build_page_with_bad_log.status_code != 200:
            raise RuntimeError(
                f"build page should tolerate non-utf8 logs, status={build_page_with_bad_log.status_code}"
            )
        if "invalid-utf8.log" not in build_page_with_bad_log.text:
            raise RuntimeError("build page did not render non-utf8 log entry")
        preview_page_with_bad_log = client.get("/problems/sample/alice/preview", params={"preview_id": preview_id})
        if preview_page_with_bad_log.status_code != 200:
            raise RuntimeError(
                f"preview page should tolerate non-utf8 latex logs, status={preview_page_with_bad_log.status_code}"
            )
        if "latex.log" not in preview_page_with_bad_log.text:
            raise RuntimeError("preview page did not render latex.log section for non-utf8 log")
    manifest = json.loads((Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "manifest.json").read_text(encoding="utf-8"))
    generation_params = manifest.get("generation_params", {})
    if int(generation_params.get("max_passes", 0)) != 8:
        raise RuntimeError(f"manifest generation_params missing max_passes: {generation_params}")
    if int(generation_params.get("compile_jobs", 0)) != 3:
        raise RuntimeError(f"manifest generation_params missing compile_jobs: {generation_params}")
    if int(generation_params.get("validate_jobs", 0)) != 2:
        raise RuntimeError(f"manifest generation_params missing validate_jobs: {generation_params}")
    if int(generation_params.get("validate_jobs_effective", 0)) != 2:
        raise RuntimeError(f"manifest generation_params missing validate_jobs_effective: {generation_params}")
    if int(generation_params.get("solve_jobs", 0)) != 2:
        raise RuntimeError(f"manifest generation_params missing solve_jobs: {generation_params}")
    if int(generation_params.get("solve_jobs_effective", 0)) != 2:
        raise RuntimeError(f"manifest generation_params missing solve_jobs_effective: {generation_params}")
    if int(generation_params.get("run_jobs", 0)) != 2:
        raise RuntimeError(f"manifest generation_params missing run_jobs: {generation_params}")
    if int(generation_params.get("run_timeout_sec", 0)) != 1:
        raise RuntimeError(f"manifest generation_params missing run_timeout_sec: {generation_params}")
    if int(manifest.get("summary", {}).get("tests_count", -1)) != 2:
        raise RuntimeError("manual sidecar files should not be treated as test inputs")
    compile_log = (Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "logs" / "compile.log").read_text(encoding="utf-8")
    if "compile_jobs=3" not in compile_log:
        raise RuntimeError("compile log missing configured compile_jobs marker")
    validate_log = (Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "logs" / "validate.log").read_text(encoding="utf-8")
    if "validate_jobs=2" not in validate_log:
        raise RuntimeError("validate log missing configured validate_jobs marker")
    solve_log = (Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "logs" / "solve.log").read_text(encoding="utf-8")
    if "solve_jobs=2" not in solve_log:
        raise RuntimeError("solve log missing configured solve_jobs marker")
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
    if int(ws_summary.get("run_config", {}).get("run_timeout_sec", 0)) != 1:
        raise RuntimeError("run config did not preserve run_timeout_sec=1")
    if ws_summary.get("feedback_dir") != "feedback_dir":
        raise RuntimeError("run summary should expose feedback_dir as repository-relative path")
    run_id_tle = run_service.run_submission("sample", "alice", build_id, submission_path="solutions/slow.cpp", mode="pass-fail")
    rrow_tle = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_tle])
    if rrow_tle is None or rrow_tle["status"] != "ok":
        raise RuntimeError(f"timeout run should complete with per-test verdicts: {rrow_tle}")
    tle_summary = json.loads(rrow_tle["summary_json"])
    if not tle_summary.get("tests") or tle_summary["tests"][0].get("verdict") != "TLE":
        raise RuntimeError(f"timeout run did not produce TLE verdicts: {tle_summary}")
    build_id_symlink_inputs = build_service.run_build("sample", "alice")
    brow_symlink_inputs = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id_symlink_inputs])
    if brow_symlink_inputs is None or brow_symlink_inputs["status"] != "ok":
        raise RuntimeError(f"symlink-input build failed unexpectedly: {brow_symlink_inputs}")
    symlink_input_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id_symlink_inputs / "tests"
    symlink_input_source = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"run-input-leak-{uuid.uuid4().hex[:8]}.txt"
    symlink_input_source.write_text("9\n", encoding="utf-8")
    symlink_input_name = f"999.run-symlink-{uuid.uuid4().hex[:6]}.in"
    symlink_input_link = symlink_input_root / symlink_input_name
    symlink_input_supported = True
    try:
        symlink_input_link.symlink_to(symlink_input_source)
    except (OSError, NotImplementedError):
        symlink_input_supported = False
    if symlink_input_supported:
        run_id_symlink_input = run_service.run_submission(
            "sample",
            "alice",
            build_id_symlink_inputs,
            submission_path="solutions/main.cpp",
            mode="pass-fail",
        )
        rrow_symlink_input = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_symlink_input])
        if rrow_symlink_input is None or rrow_symlink_input["status"] != "ok":
            raise RuntimeError(f"run with symlinked test input should still pass using safe tests only: {rrow_symlink_input}")
        symlink_input_summary = json.loads(rrow_symlink_input["summary_json"])
        tests_seen = [str(t.get("test", "")) for t in symlink_input_summary.get("tests", []) if isinstance(t, dict)]
        if symlink_input_name in tests_seen:
            raise RuntimeError("run execution should ignore symlinked test inputs in build artifacts")
    symlink_input_link.unlink(missing_ok=True)
    symlink_input_source.unlink(missing_ok=True)
    bad_json_build_id = f"b-badjson-{uuid.uuid4().hex[:8]}"
    bad_json_run_id = f"r-badjson-{uuid.uuid4().hex[:8]}"
    build_artifact_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id
    run_artifact_row = db.fetch_one("SELECT artifact_path FROM runs WHERE id=?", [run_id_ws])
    if run_artifact_row is None:
        raise RuntimeError("workspace run artifact row missing")
    db.execute(
        "INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            bad_json_build_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "failed",
            "{bad",
            str(build_artifact_root),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    db.execute(
        "INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            bad_json_run_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            build_id,
            "pass-fail",
            "failed",
            "{bad",
            str(run_artifact_row["artifact_path"]),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    bad_root_build_id = f"b-badroot-{uuid.uuid4().hex[:8]}"
    poison_marker = f"poison-build-log-{uuid.uuid4().hex[:8]}"
    poison_log = ws / "logs" / f"{poison_marker}.log"
    poison_log.parent.mkdir(parents=True, exist_ok=True)
    poison_log.write_text(poison_marker + "\n", encoding="utf-8")
    db.execute(
        "INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            bad_root_build_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "failed",
            json.dumps({"error": "injected poisoned build artifact path"}),
            str(ws),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    dot_build_id = "."
    dot_root_marker = f"dot-build-root-log-{uuid.uuid4().hex[:8]}"
    dot_root_log = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / "logs" / f"{dot_root_marker}.log"
    dot_root_log.parent.mkdir(parents=True, exist_ok=True)
    dot_root_log.write_text(dot_root_marker + "\n", encoding="utf-8")
    db.execute("DELETE FROM builds WHERE id=?", [dot_build_id])
    db.execute(
        "INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            dot_build_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "failed",
            json.dumps({"error": "injected dotted build id"}),
            str(Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample"),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    with TestClient(app) as client:
        bad_build_page = client.get("/problems/sample/alice/build", params={"build_id": bad_json_build_id})
        if bad_build_page.status_code != 200:
            raise RuntimeError(f"build page should handle malformed summary_json, status={bad_build_page.status_code}")
        if "invalid summary_json for build" not in bad_build_page.text:
            raise RuntimeError("build page did not surface malformed summary_json fallback")
        poisoned_build_page = client.get("/problems/sample/alice/build", params={"build_id": bad_root_build_id})
        if poisoned_build_page.status_code != 200:
            raise RuntimeError(
                f"build page should tolerate poisoned artifact path metadata, status={poisoned_build_page.status_code}"
            )
        if poison_marker in poisoned_build_page.text:
            raise RuntimeError("build page should not read logs from DB-provided artifact_path")
        dot_build_page = client.get("/problems/sample/alice/build", params={"build_id": dot_build_id})
        if dot_build_page.status_code != 200:
            raise RuntimeError(f"build page should tolerate dotted build ids, status={dot_build_page.status_code}")
        if dot_root_marker in dot_build_page.text:
            raise RuntimeError("build page should not resolve dotted build ids to problem artifact root")
        dot_artifact_browse = client.get("/problems/sample/alice/artifacts/%2E/browse", params={"rel": "logs"})
        if dot_artifact_browse.status_code != 404:
            raise RuntimeError("artifact browse should reject dotted build ids")
        dot_manifest = client.get("/api/problems/sample/workspaces/alice/builds/%2E/manifest")
        if dot_manifest.status_code != 404:
            raise RuntimeError("workspace manifest endpoint should reject dotted build ids")
        bad_run_page = client.get("/problems/sample/alice/run", params={"run_id": bad_json_run_id})
        if bad_run_page.status_code != 200:
            raise RuntimeError(f"run page should handle malformed summary_json, status={bad_run_page.status_code}")
        if "invalid summary_json for run" not in bad_run_page.text:
            raise RuntimeError("run page did not surface malformed summary_json fallback")
    poison_log.unlink(missing_ok=True)
    dot_root_log.unlink(missing_ok=True)

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
    run_id_upload_empty = run_service.run_submission(
        "sample",
        "alice",
        build_id,
        mode="pass-fail",
        upload_content=b"",
        upload_filename="empty-upload.cpp",
    )
    rrow_upload_empty = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_upload_empty])
    if rrow_upload_empty is None or rrow_upload_empty["status"] != "failed":
        raise RuntimeError(f"empty upload run should fail compilation (not missing submission_path): {rrow_upload_empty}")
    upload_empty_summary = json.loads(rrow_upload_empty["summary_json"])
    if upload_empty_summary.get("error") != "compile_error":
        raise RuntimeError(f"empty upload run should report compile_error: {upload_empty_summary}")
    if upload_empty_summary.get("source") != "empty-upload.cpp":
        raise RuntimeError("empty upload run did not preserve uploaded filename in summary source")
    with TestClient(app) as client:
        route_upload_resp = client.post(
            "/problems/sample/alice/run/execute",
            data={"build_id": build_id, "mode": "pass-fail", "submission_path": ""},
            files={"submission_upload": ("upload-route.cpp", upload_src, "text/x-c++src")},
            follow_redirects=False,
        )
        if route_upload_resp.status_code != 303:
            raise RuntimeError(f"run execute upload route should redirect, status={route_upload_resp.status_code}")
        location = route_upload_resp.headers.get("location", "")
        if "run_id=" not in location:
            raise RuntimeError(f"run execute upload route missing run_id redirect parameter: {location}")
        run_id_upload_route = location.split("run_id=", 1)[1].split("&", 1)[0]
        route_path_with_empty_upload_resp = client.post(
            "/problems/sample/alice/run/execute",
            data={"build_id": build_id, "mode": "pass-fail", "submission_path": "solutions/main.cpp"},
            files={"submission_upload": ("   ", b"", "application/octet-stream")},
            follow_redirects=False,
        )
        if route_path_with_empty_upload_resp.status_code != 303:
            raise RuntimeError(
                "run execute route with empty upload filename should redirect"
                f", status={route_path_with_empty_upload_resp.status_code}"
            )
        location_path_empty = route_path_with_empty_upload_resp.headers.get("location", "")
        if "run_id=" not in location_path_empty:
            raise RuntimeError(
                "run execute route with empty upload filename missing run_id redirect parameter:"
                f" {location_path_empty}"
            )
        run_id_path_with_empty_upload = location_path_empty.split("run_id=", 1)[1].split("&", 1)[0]
        route_upload_empty_resp = client.post(
            "/problems/sample/alice/run/execute",
            data={"build_id": build_id, "mode": "pass-fail", "submission_path": ""},
            files={"submission_upload": ("empty-route.cpp", b"", "text/x-c++src")},
            follow_redirects=False,
        )
        if route_upload_empty_resp.status_code != 303:
            raise RuntimeError(
                f"run execute empty-upload route should redirect, status={route_upload_empty_resp.status_code}"
            )
        location_empty = route_upload_empty_resp.headers.get("location", "")
        if "run_id=" not in location_empty:
            raise RuntimeError(f"run execute empty-upload route missing run_id redirect parameter: {location_empty}")
        run_id_upload_empty_route = location_empty.split("run_id=", 1)[1].split("&", 1)[0]
        invalid_mode_resp = client.post(
            "/problems/sample/alice/run/execute",
            data={"build_id": build_id, "mode": "bogus-mode", "submission_path": "solutions/main.cpp"},
            follow_redirects=False,
        )
        if invalid_mode_resp.status_code != 303:
            raise RuntimeError(f"run execute invalid-mode route should redirect, status={invalid_mode_resp.status_code}")
        location_invalid_mode = invalid_mode_resp.headers.get("location", "")
        if "run_id=" not in location_invalid_mode:
            raise RuntimeError(f"run execute invalid-mode route missing run_id redirect parameter: {location_invalid_mode}")
        run_id_invalid_mode_route = location_invalid_mode.split("run_id=", 1)[1].split("&", 1)[0]
    rrow_upload_route = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_upload_route])
    if rrow_upload_route is None or rrow_upload_route["status"] != "ok":
        raise RuntimeError(f"upload run via route failed: {rrow_upload_route}")
    upload_route_summary = json.loads(rrow_upload_route["summary_json"])
    if upload_route_summary.get("source") != "upload-route.cpp":
        raise RuntimeError("upload run via route did not preserve uploaded filename in summary source")
    rrow_path_with_empty_upload = db.fetch_one(
        "SELECT status,summary_json FROM runs WHERE id=?",
        [run_id_path_with_empty_upload],
    )
    if rrow_path_with_empty_upload is None or rrow_path_with_empty_upload["status"] != "ok":
        raise RuntimeError(
            "run execute route with empty upload filename should execute using submission_path"
            f": {rrow_path_with_empty_upload}"
        )
    path_with_empty_upload_summary = json.loads(rrow_path_with_empty_upload["summary_json"])
    if path_with_empty_upload_summary.get("source") != "solutions/main.cpp":
        raise RuntimeError("route with empty upload filename did not preserve submission_path source")
    rrow_upload_empty_route = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_upload_empty_route])
    if rrow_upload_empty_route is None or rrow_upload_empty_route["status"] != "failed":
        raise RuntimeError(f"empty upload run via route should fail compilation: {rrow_upload_empty_route}")
    upload_empty_route_summary = json.loads(rrow_upload_empty_route["summary_json"])
    if upload_empty_route_summary.get("error") != "compile_error":
        raise RuntimeError(f"empty upload run via route should report compile_error: {upload_empty_route_summary}")
    if upload_empty_route_summary.get("source") != "empty-route.cpp":
        raise RuntimeError("empty upload run via route did not preserve uploaded filename in summary source")
    rrow_invalid_mode_route = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_invalid_mode_route])
    if rrow_invalid_mode_route is None or rrow_invalid_mode_route["status"] != "failed":
        raise RuntimeError(f"invalid run mode via route should produce failed run metadata: {rrow_invalid_mode_route}")
    invalid_mode_route_summary = json.loads(rrow_invalid_mode_route["summary_json"])
    if "unsupported run mode" not in str(invalid_mode_route_summary.get("error", "")):
        raise RuntimeError(f"invalid run mode route failure reason missing detail: {invalid_mode_route_summary}")

    run_id_invalid_mode_service = run_service.run_submission(
        "sample",
        "alice",
        build_id,
        submission_path="solutions/main.cpp",
        mode="bogus-mode",
    )
    rrow_invalid_mode_service = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id_invalid_mode_service])
    if rrow_invalid_mode_service is None or rrow_invalid_mode_service["status"] != "failed":
        raise RuntimeError(f"invalid run mode via service should produce failed run metadata: {rrow_invalid_mode_service}")
    invalid_mode_service_summary = json.loads(rrow_invalid_mode_service["summary_json"])
    if "unsupported run mode" not in str(invalid_mode_service_summary.get("error", "")):
        raise RuntimeError(f"invalid run mode service failure reason missing detail: {invalid_mode_service_summary}")

    run_id_multi = run_service.run_submission("sample", "alice", build_id, submission_path="solutions/main.cpp", mode="multi-pass")
    rrow_multi = db.fetch_one("SELECT status,summary_json,artifact_path FROM runs WHERE id=?", [run_id_multi])
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
    if interactive_summary.get("feedback_dir") != "feedback_dir":
        raise RuntimeError("interactive run summary should expose feedback_dir as repository-relative path")

    run_id_interactive_re = run_service.run_submission(
        "sample",
        "alice",
        build_id,
        submission_path="solutions/re_interactive.cpp",
        mode="interactive",
    )
    rrow_interactive_re = db.fetch_one("SELECT status,summary_json,artifact_path FROM runs WHERE id=?", [run_id_interactive_re])
    if rrow_interactive_re is None or rrow_interactive_re["status"] != "ok":
        raise RuntimeError(f"interactive RE run failed unexpectedly: {rrow_interactive_re}")
    interactive_re_summary = json.loads(rrow_interactive_re["summary_json"])
    if not interactive_re_summary.get("tests") or interactive_re_summary["tests"][0].get("verdict") != "RE":
        raise RuntimeError("interactive RE run did not produce RE verdict")
    first_test = interactive_re_summary["tests"][0]["test"]
    interactive_re_transcript = Path(rrow_interactive_re["artifact_path"]) / f"{Path(first_test).stem}.transcript.txt"
    transcript_text = interactive_re_transcript.read_text(encoding="utf-8")
    if "submission stderr:" not in transcript_text or "interactive forced re" not in transcript_text:
        raise RuntimeError("interactive RE run transcript missing captured submission stderr")

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
    if traversal_summary.get("feedback_dir") != "feedback_dir":
        raise RuntimeError("failed run summary should expose feedback_dir as repository-relative path")

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
    poisoned_build_id = f"../poisoned-build-{uuid.uuid4().hex[:8]}"
    poisoned_leaf = poisoned_build_id.split("/")[-1]
    poisoned_root = (Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / poisoned_leaf).resolve()
    (poisoned_root / "tests").mkdir(parents=True, exist_ok=True)
    (poisoned_root / "ans").mkdir(parents=True, exist_ok=True)
    (poisoned_root / "logs").mkdir(parents=True, exist_ok=True)
    (poisoned_root / "manifest.json").write_text("{}", encoding="utf-8")
    (poisoned_root / "tests/001.in").write_text("1\n", encoding="utf-8")
    (poisoned_root / "ans/001.ans").write_text("1\n", encoding="utf-8")
    db.execute("DELETE FROM builds WHERE id=?", [poisoned_build_id])
    db.execute(
        "INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            poisoned_build_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            head_commit,
            ctx["workspace"].get("branch") or "main",
            "ok",
            json.dumps({"error": "injected traversal-style build id"}),
            str(poisoned_root),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    run_id_poisoned_build = run_service.run_submission(
        "sample",
        "alice",
        poisoned_build_id,
        submission_path="solutions/main.cpp",
        mode="pass-fail",
    )
    rrow_poisoned_build = db.fetch_one("SELECT status,summary_json,artifact_path FROM runs WHERE id=?", [run_id_poisoned_build])
    if rrow_poisoned_build is None or rrow_poisoned_build["status"] != "failed":
        raise RuntimeError(f"traversal-style build id run should fail preflight: {rrow_poisoned_build}")
    poisoned_build_summary = json.loads(rrow_poisoned_build["summary_json"])
    if "invalid build artifact id" not in str(poisoned_build_summary.get("error", "")):
        raise RuntimeError("traversal-style build id run did not report invalid build artifact id")
    if invalid_runs_root.resolve() not in Path(rrow_poisoned_build["artifact_path"]).resolve().parents:
        raise RuntimeError("traversal-style build id run was not isolated under run_root/invalid-runs")
    try:
        export_service.create_export("sample", poisoned_build_id, "polygon-standard")
        raise RuntimeError("export should reject traversal-style build ids before artifact resolution")
    except ValueError as exc:
        if "invalid build artifact id" not in str(exc):
            raise RuntimeError(f"traversal-style build id export rejection reason mismatch: {exc}")
    shutil.rmtree(poisoned_root, ignore_errors=True)
    bad_root_run_id = f"r-badroot-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            bad_root_run_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            build_id,
            "pass-fail",
            "failed",
            json.dumps({"error": "injected bad artifact root"}),
            str(ws),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    bad_nested_run_id = f"r-badnested-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            bad_nested_run_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            build_id,
            "pass-fail",
            "failed",
            json.dumps({"error": "injected nested artifact root"}),
            str((Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "logs").resolve()),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    bad_buildid_run_id = f"r-badbuildid-{uuid.uuid4().hex[:8]}"
    bad_buildid_root = (Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / "logs" / f"run-{bad_buildid_run_id}").resolve()
    bad_buildid_root.mkdir(parents=True, exist_ok=True)
    (bad_buildid_root / "compile.log").write_text("poisoned build-id path\n", encoding="utf-8")
    db.execute(
        "INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            bad_buildid_run_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            "../poison",
            "pass-fail",
            "failed",
            json.dumps({"error": "injected traversal-style build id"}),
            str(bad_buildid_root),
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
        ],
    )
    with TestClient(app) as client:
        valid_summary_file = client.get(f"/problems/sample/alice/runs/{run_id_multi}/artifacts/summary.json")
        if valid_summary_file.status_code != 200:
            raise RuntimeError(f"run artifact summary fetch failed status={valid_summary_file.status_code}")
        valid_feedback_zip = client.get(
            f"/problems/sample/alice/runs/{run_id_multi}/download-dir",
            params={"rel": "feedback_dir"},
        )
        if valid_feedback_zip.status_code != 200 or valid_feedback_zip.headers.get("content-type", "").find("zip") == -1:
            raise RuntimeError(f"run artifact feedback zip failed status={valid_feedback_zip.status_code}")
        invalid_compile_file = client.get(f"/problems/sample/alice/runs/{run_id_bad_build}/artifacts/compile.log")
        if invalid_compile_file.status_code != 200:
            raise RuntimeError(f"invalid run compile.log should be readable status={invalid_compile_file.status_code}")
        if "build not runnable" not in invalid_compile_file.text:
            raise RuntimeError("invalid run compile.log missing preflight failure reason")
        invalid_summary_file = client.get(f"/problems/sample/alice/runs/{run_id_bad_build}/artifacts/summary.json")
        if invalid_summary_file.status_code != 200:
            raise RuntimeError(f"invalid run summary.json should be readable status={invalid_summary_file.status_code}")
        poisoned_artifact = client.get(f"/problems/sample/alice/runs/{bad_root_run_id}/artifacts/README.problem.md")
        if poisoned_artifact.status_code != 404:
            raise RuntimeError(
                "run artifact endpoint should reject DB-poisoned artifact roots outside allowed run/artifact trees"
            )
        poisoned_browse = client.get(
            f"/problems/sample/alice/runs/{bad_root_run_id}/browse",
            params={"rel": "."},
        )
        if poisoned_browse.status_code != 404:
            raise RuntimeError("run artifact browse should reject DB-poisoned artifact roots")
        poisoned_nested_artifact = client.get(f"/problems/sample/alice/runs/{bad_nested_run_id}/artifacts/compile.log")
        if poisoned_nested_artifact.status_code != 404:
            raise RuntimeError("run artifact endpoint should reject DB-poisoned nested artifact roots")
        poisoned_buildid_artifact = client.get(f"/problems/sample/alice/runs/{bad_buildid_run_id}/artifacts/compile.log")
        if poisoned_buildid_artifact.status_code != 404:
            raise RuntimeError("run artifact endpoint should reject DB-poisoned traversal-style build_id roots")
    run_leak_src = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"run-zip-leak-{uuid.uuid4().hex[:8]}.txt"
    run_leak_src.write_text("run-leak\n", encoding="utf-8")
    run_escape_name = "999.run-escape.txt"
    run_feedback_root = Path(rrow_multi["artifact_path"]) / "feedback_dir"
    run_escape_link = run_feedback_root / run_escape_name
    run_symlink_supported = True
    try:
        run_escape_link.symlink_to(run_leak_src)
    except (OSError, NotImplementedError):
        run_symlink_supported = False
    if run_symlink_supported:
        try:
            with TestClient(app) as client:
                run_browse = client.get(
                    f"/problems/sample/alice/runs/{run_id_multi}/browse",
                    params={"rel": "feedback_dir"},
                )
                if run_browse.status_code != 200:
                    raise RuntimeError(f"run artifact browse failed during symlink hardening check: {run_browse.status_code}")
                if run_escape_name in run_browse.text:
                    raise RuntimeError("run artifact browse leaked symlink escape entry")
                run_zip = client.get(
                    f"/problems/sample/alice/runs/{run_id_multi}/download-dir",
                    params={"rel": "feedback_dir"},
                )
                if run_zip.status_code != 200:
                    raise RuntimeError(f"run artifact zip failed during symlink hardening check: {run_zip.status_code}")
                with zipfile.ZipFile(BytesIO(run_zip.content)) as zf:
                    if any(Path(name).name == run_escape_name for name in zf.namelist()):
                        raise RuntimeError("run artifact zip included symlink escape entry")
        finally:
            run_escape_link.unlink(missing_ok=True)
    run_leak_src.unlink(missing_ok=True)

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

    build_id_missing_tests_dir = build_service.run_build("sample", "alice")
    brow_missing_tests_dir = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id_missing_tests_dir])
    if brow_missing_tests_dir is None or brow_missing_tests_dir["status"] != "ok":
        raise RuntimeError(f"missing-tests-dir build failed unexpectedly: {brow_missing_tests_dir}")
    missing_tests_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id_missing_tests_dir
    shutil.rmtree(missing_tests_root / "tests", ignore_errors=True)
    run_id_missing_tests_dir = run_service.run_submission(
        "sample",
        "alice",
        build_id_missing_tests_dir,
        submission_path="solutions/main.cpp",
        mode="pass-fail",
    )
    rrow_missing_tests_dir = db.fetch_one("SELECT status,summary_json,artifact_path FROM runs WHERE id=?", [run_id_missing_tests_dir])
    if rrow_missing_tests_dir is None or rrow_missing_tests_dir["status"] != "failed":
        raise RuntimeError(f"missing-tests-dir run should fail: {rrow_missing_tests_dir}")
    missing_tests_summary = json.loads(rrow_missing_tests_dir["summary_json"])
    if "artifact directory missing: tests/" not in str(missing_tests_summary.get("error", "")):
        raise RuntimeError("missing-tests-dir run did not report required artifact-directory preflight failure")
    if invalid_runs_root.resolve() not in Path(rrow_missing_tests_dir["artifact_path"]).resolve().parents:
        raise RuntimeError("missing-tests-dir run was not isolated under run_root/invalid-runs")

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

    export_symlink_problem = f"exportsymlink-{uuid.uuid4().hex[:8]}"
    export_symlink_user = f"u-{uuid.uuid4().hex[:6]}"
    workspace_service.ensure_problem(export_symlink_problem, "Export Symlink Hardening Problem")
    workspace_service.ensure_workspace(export_symlink_problem, export_symlink_user)
    export_symlink_ctx = workspace_service.workspace_context(export_symlink_problem, export_symlink_user)
    export_symlink_ws = Path(export_symlink_ctx["workspace"]["path"])
    for d in ["solutions", "validators", "checkers", "tests/manual", "config", "statement"]:
        (export_symlink_ws / d).mkdir(parents=True, exist_ok=True)
    (export_symlink_ws / "tests/manual/001.in").write_text("1\n", encoding="utf-8")
    (export_symlink_ws / "solutions/accepted.cpp").write_text(
        "#include <bits/stdc++.h>\nusing namespace std; int main(){ long long x=0; if(cin>>x) cout<<x<<\"\\n\"; }\n",
        encoding="utf-8",
    )
    (export_symlink_ws / "validators/validator.cpp").write_text(
        "#include <bits/stdc++.h>\nint main(){return 42;}\n",
        encoding="utf-8",
    )
    (export_symlink_ws / "checkers/checker.cpp").write_text(
        "#include <bits/stdc++.h>\nint main(){return 42;}\n",
        encoding="utf-8",
    )
    (export_symlink_ws / "config/build.json").write_text(
        json.dumps({"generator_runs": 0}),
        encoding="utf-8",
    )
    (export_symlink_ws / "statement/main.tex").write_text(
        "\\documentclass{article}\\begin{document}export\\end{document}\n",
        encoding="utf-8",
    )
    export_symlink_source = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"export-link-source-{uuid.uuid4().hex[:8]}.txt"
    export_symlink_source.write_text("export-symlink-leak\n", encoding="utf-8")
    export_symlink_supported = True
    try:
        (export_symlink_ws / "statement/leak.tex").symlink_to(export_symlink_source)
    except (OSError, NotImplementedError):
        export_symlink_supported = False
    for cmd in [
        ["git", "-C", str(export_symlink_ws), "config", "user.email", "smoke@polygonlike.local"],
        ["git", "-C", str(export_symlink_ws), "config", "user.name", "Smoke Test"],
        ["git", "-C", str(export_symlink_ws), "add", "."],
        ["git", "-C", str(export_symlink_ws), "commit", "-m", "seed export symlink smoke data"],
    ]:
        proc = run_cmd(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"export symlink setup command failed: {' '.join(cmd)}")
    if export_symlink_supported:
        export_symlink_build_id = build_service.run_build(export_symlink_problem, export_symlink_user)
        export_symlink_build_row = db.fetch_one("SELECT status FROM builds WHERE id=?", [export_symlink_build_id])
        if export_symlink_build_row is None or export_symlink_build_row["status"] != "ok":
            raise RuntimeError(f"export symlink hardening build failed: {export_symlink_build_row}")
        export_symlink_zip = export_service.create_export(export_symlink_problem, export_symlink_build_id, "kattis")
        export_symlink_entries = _zip_entries(export_symlink_zip)
        if _has_suffix(export_symlink_entries, "statement/leak.tex"):
            raise RuntimeError("kattis export should not copy symlinked statement assets from source snapshot")
    export_symlink_source.unlink(missing_ok=True)

    export_artifact_leak_source = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"export-artifact-leak-{uuid.uuid4().hex[:8]}.txt"
    export_artifact_leak_source.write_text("export-artifact-leak\n", encoding="utf-8")
    export_artifact_escape_name = f"999.export-escape-{uuid.uuid4().hex[:6]}.in"
    export_artifact_escape_link = (
        Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "tests" / export_artifact_escape_name
    )
    export_artifact_symlink_supported = True
    try:
        export_artifact_escape_link.symlink_to(export_artifact_leak_source)
    except (OSError, NotImplementedError):
        export_artifact_symlink_supported = False

    export_outputs: dict[str, Path] = {}
    for export_type in ["kattis", "domjudge", "polygon-standard", "polygon-full"]:
        out = export_service.create_export("sample", build_id, export_type)
        if not out.exists():
            raise RuntimeError(f"missing export {export_type}")
        export_outputs[export_type] = out

    second_kattis = export_service.create_export("sample", build_id, "kattis")
    if not second_kattis.exists():
        raise RuntimeError("second kattis export missing")
    if second_kattis.name == export_outputs["kattis"].name:
        raise RuntimeError("duplicate kattis export overwrote previous filename")
    if not export_outputs["kattis"].exists():
        raise RuntimeError("first kattis export should still exist after second export")
    recent_kattis = db.fetch_all(
        "SELECT filename FROM exports WHERE problem_id=? AND build_id=? AND export_type='kattis' ORDER BY created_at DESC LIMIT 2",
        [ctx["problem"]["id"], build_id],
    )
    if len(recent_kattis) < 2:
        raise RuntimeError("expected at least two kattis export records")
    if str(recent_kattis[0]["filename"]) == str(recent_kattis[1]["filename"]):
        raise RuntimeError("kattis export records should preserve distinct filenames per generation")

    build_id_polygon_snapshotless = build_service.run_build("sample", "alice")
    brow_polygon_snapshotless = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id_polygon_snapshotless])
    if brow_polygon_snapshotless is None or brow_polygon_snapshotless["status"] != "ok":
        raise RuntimeError(f"polygon snapshotless build failed unexpectedly: {brow_polygon_snapshotless}")
    build_id_missing_source_commit = build_service.run_build("sample", "alice")
    brow_missing_source_commit = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id_missing_source_commit])
    if brow_missing_source_commit is None or brow_missing_source_commit["status"] != "ok":
        raise RuntimeError(f"missing-source-commit build failed unexpectedly: {brow_missing_source_commit}")
    db.execute("UPDATE builds SET source_commit=? WHERE id=?", ["", build_id_missing_source_commit])
    try:
        export_service.create_export("sample", build_id_missing_source_commit, "kattis")
        raise RuntimeError("kattis export should fail when build source_commit is missing")
    except ValueError as exc:
        if "source_commit missing" not in str(exc):
            raise RuntimeError(f"kattis missing-source-commit export rejection reason mismatch: {exc}")
    poly_missing_source_commit = export_service.create_export("sample", build_id_missing_source_commit, "polygon-standard")
    if not poly_missing_source_commit.exists():
        raise RuntimeError("polygon-standard export should not require source_commit metadata")
    db.execute("UPDATE builds SET source_commit=? WHERE id=?", ["deadbeefdeadbeef", build_id_polygon_snapshotless])
    poly_snapshotless = export_service.create_export("sample", build_id_polygon_snapshotless, "polygon-standard")
    if not poly_snapshotless.exists():
        raise RuntimeError("polygon-standard export should not depend on source snapshot reconstruction")
    try:
        export_service.create_export("sample", build_id_polygon_snapshotless, "kattis")
        raise RuntimeError("kattis export should fail when source snapshot reconstruction fails")
    except ValueError:
        pass

    kattis_entries = _zip_entries(export_outputs["kattis"])
    _expect_suffix(kattis_entries, "problem.yaml", "kattis")
    _expect_suffix(kattis_entries, "statement/problem.en.tex", "kattis")
    _expect_suffix(kattis_entries, "data/secret/001.in", "kattis")
    _expect_suffix(kattis_entries, "submissions/accepted/accepted.cpp", "kattis")
    _expect_suffix(kattis_entries, "input_validators/validator.cpp", "kattis")
    if export_artifact_symlink_supported and _has_suffix(kattis_entries, f"data/secret/{export_artifact_escape_name}"):
        raise RuntimeError("kattis export should not copy symlinked tests from build artifacts")

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
    if export_artifact_symlink_supported and _has_suffix(polygon_full_entries, f"tests/{export_artifact_escape_name}"):
        raise RuntimeError("polygon-full export should not copy symlinked tests from build artifacts")
    export_artifact_escape_link.unlink(missing_ok=True)
    export_artifact_leak_source.unlink(missing_ok=True)

    with TestClient(app) as client:
        r = client.get(
            f"/problems/sample/alice/artifacts/{build_id}/download-dir",
            params={"rel": "tests"},
        )
        if r.status_code != 200 or r.headers.get("content-type", "").find("zip") == -1:
            raise RuntimeError(f"download-dir failed status={r.status_code}")
    leak_src = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"zip-leak-{uuid.uuid4().hex[:8]}.txt"
    leak_src.write_text("leak-check\n", encoding="utf-8")
    leak_dir = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"zip-leak-dir-{uuid.uuid4().hex[:8]}"
    leak_dir.mkdir(parents=True, exist_ok=True)
    leak_dir_file = leak_dir / "hidden.out"
    leak_dir_file.write_text("hidden\n", encoding="utf-8")
    escape_name = "999.escape.in"
    escape_link = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "tests" / escape_name
    escape_dir_name = "998.escape-dir"
    escape_dir_link = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id / "tests" / escape_dir_name
    symlink_supported = True
    try:
        escape_link.symlink_to(leak_src)
        escape_dir_link.symlink_to(leak_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        symlink_supported = False
    if symlink_supported:
        try:
            with TestClient(app) as client:
                browse_resp = client.get(
                    f"/problems/sample/alice/artifacts/{build_id}/browse",
                    params={"rel": "tests"},
                )
                if browse_resp.status_code != 200:
                    raise RuntimeError(f"artifact browse failed during symlink hardening check: {browse_resp.status_code}")
                if escape_name in browse_resp.text:
                    raise RuntimeError("artifact browse leaked symlink escape entry")
                if escape_dir_name in browse_resp.text:
                    raise RuntimeError("artifact browse leaked symlink directory escape entry")
                zip_resp = client.get(
                    f"/problems/sample/alice/artifacts/{build_id}/download-dir",
                    params={"rel": "tests"},
                )
                if zip_resp.status_code != 200:
                    raise RuntimeError(f"artifact zip failed during symlink hardening check: {zip_resp.status_code}")
                with zipfile.ZipFile(BytesIO(zip_resp.content)) as zf:
                    names = zf.namelist()
                    if any(Path(name).name == escape_name for name in names):
                        raise RuntimeError("artifact zip included symlink escape entry")
                    if any(escape_dir_name in Path(name).parts for name in names):
                        raise RuntimeError("artifact zip included symlink directory escape entry")
        finally:
            escape_link.unlink(missing_ok=True)
            escape_dir_link.unlink(missing_ok=True)
    leak_src.unlink(missing_ok=True)
    shutil.rmtree(leak_dir, ignore_errors=True)

    workspace_service.ensure_workspace("sample", "bob")
    bob_preview_id = preview_service.compile_preview("sample", "bob")
    bob_build_id = build_service.run_build("sample", "bob")
    bob_run_id = run_service.run_submission(
        "sample",
        "bob",
        "b-does-not-exist",
        submission_path="solutions/main.cpp",
        mode="pass-fail",
    )
    alice_cross_run_id = run_service.run_submission(
        "sample",
        "alice",
        bob_build_id,
        submission_path="solutions/main.cpp",
        mode="pass-fail",
    )
    alice_cross_run = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [alice_cross_run_id])
    if alice_cross_run is None or alice_cross_run["status"] != "failed":
        raise RuntimeError(f"cross-workspace run should fail: {alice_cross_run}")
    alice_cross_summary = json.loads(alice_cross_run["summary_json"]) if alice_cross_run["summary_json"] else {}
    if "selected workspace" not in str(alice_cross_summary.get("error", "")):
        raise RuntimeError("cross-workspace run did not report workspace ownership preflight failure")
    bob_export_id = f"e-{uuid.uuid4().hex[:10]}"
    db.execute(
        "INSERT INTO exports(id,problem_id,build_id,export_type,filename,sha256,size_bytes,source_commit,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            bob_export_id,
            ctx["problem"]["id"],
            bob_build_id,
            "polygon-standard",
            "bob-fake.zip",
            "0" * 64,
            0,
            "",
            "1970-01-01T00:00:00Z",
        ],
    )
    with TestClient(app) as client:
        builds_json = client.get("/api/problems/sample/workspaces/alice/recent-builds").json()
        previews_json = client.get("/api/problems/sample/workspaces/alice/recent-previews").json()
        runs_json = client.get("/api/problems/sample/workspaces/alice/recent-runs").json()
        exports_json = client.get("/api/problems/sample/workspaces/alice/recent-exports").json()
        if any(str(x.get("id")) == bob_build_id for x in builds_json):
            raise RuntimeError("alice recent-builds API leaked bob workspace build")
        if any(str(x.get("id")) == bob_preview_id for x in previews_json):
            raise RuntimeError("alice recent-previews API leaked bob workspace preview")
        if any(str(x.get("id")) == bob_run_id for x in runs_json):
            raise RuntimeError("alice recent-runs API leaked bob workspace run")
        if any(str(x.get("id")) == bob_export_id for x in exports_json):
            raise RuntimeError("alice recent-exports API leaked bob workspace export")
        for page, leaked_id, label in [
            ("/problems/sample/alice/build", bob_build_id, "build page"),
            ("/problems/sample/alice/preview", bob_preview_id, "preview page"),
            ("/problems/sample/alice/run", bob_run_id, "run page"),
            ("/problems/sample/alice/export", bob_export_id, "export page"),
        ]:
            resp = client.get(page)
            if resp.status_code != 200:
                raise RuntimeError(f"{label} request failed status={resp.status_code}")
            if leaked_id in resp.text:
                raise RuntimeError(f"{label} leaked bob workspace entry")
        preview_detail_leak = client.get("/problems/sample/alice/preview", params={"preview_id": bob_preview_id})
        if preview_detail_leak.status_code != 200:
            raise RuntimeError(f"preview detail leak check failed status={preview_detail_leak.status_code}")
        if bob_preview_id in preview_detail_leak.text:
            raise RuntimeError("preview page leaked bob preview detail via preview_id query")
        for path, params, label in [
            (f"/problems/sample/alice/artifacts/{bob_build_id}/browse", {"rel": "tests"}, "build artifact browse"),
            (f"/problems/sample/alice/artifacts/{bob_build_id}/download-dir", {"rel": "tests"}, "build artifact zip"),
            (f"/problems/sample/alice/artifacts/{bob_build_id}/tests/001.in", None, "build artifact file"),
            (f"/problems/sample/alice/artifacts/{bob_preview_id}/browse", {"rel": "logs"}, "preview artifact browse"),
            (f"/problems/sample/alice/runs/{bob_run_id}/artifacts/compile.log", None, "run artifact file"),
        ]:
            resp = client.get(path, params=params)
            if resp.status_code != 404:
                raise RuntimeError(f"{label} should be workspace-forbidden, got status={resp.status_code}")
        own_manifest = client.get(f"/api/problems/sample/workspaces/alice/builds/{build_id}/manifest")
        if own_manifest.status_code != 200:
            raise RuntimeError(f"workspace manifest endpoint failed status={own_manifest.status_code}")
        leaked_manifest = client.get(f"/api/problems/sample/workspaces/alice/builds/{bob_build_id}/manifest")
        if leaked_manifest.status_code != 404:
            raise RuntimeError(f"workspace manifest should not expose bob build, status={leaked_manifest.status_code}")
        export_block = client.post(
            "/problems/sample/alice/export/create",
            data={"build_id": bob_build_id, "export_type": "kattis"},
            follow_redirects=False,
        )
        if export_block.status_code != 303:
            raise RuntimeError(f"cross-workspace export should redirect with error, status={export_block.status_code}")
        location = export_block.headers.get("location", "")
        if "build+not+found+in+workspace" not in location:
            raise RuntimeError(f"cross-workspace export error mismatch location={location}")

    print("smoke_ok", preview_id, build_id, build_id_repeat, build_id_kattis, run_id_ws, run_id_upload, run_id_multi, run_id_interactive, run_id_kattis)


if __name__ == "__main__":
    main()
