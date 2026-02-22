from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import DB, now_iso
from app.services.artifact_service import ArtifactService
from app.services.build_service import BuildService
from app.services.export_service import ExportService
from app.services.git_service import GitService
from app.services.preview_service import PreviewService
from app.services.run_service import RunService
from app.services.toolchain_service import ToolchainService
from app.services.workspace_service import WorkspaceService
from app.settings import load_settings

settings = load_settings()
db = DB(settings.db_path)
workspace_service = WorkspaceService(db, settings)
git_service = GitService()
artifact_service = ArtifactService(settings.artifacts_root)
toolchain_service = ToolchainService(settings.cache_root)
build_service = BuildService(db, workspace_service, artifact_service, toolchain_service)
preview_service = PreviewService(db, workspace_service, artifact_service)
run_service = RunService(db, workspace_service)
export_service = ExportService(db, settings.artifacts_root)

app = FastAPI(title="Polygonlike")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup() -> None:
    db.init()
    workspace_service.ensure_problem("sample", "Sample Problem")
    workspace_service.ensure_workspace("sample", "alice")


def page_ctx(problem: str, user: str) -> dict:
    workspace_service.ensure_workspace(problem, user)
    workspace_service.refresh_workspace_status(problem, user)
    ctx = workspace_service.workspace_context(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        ctx["branches"] = git_service.list_branches(workspace)
    except Exception:
        ctx["branches"] = [ctx["workspace"].get("branch") or "main"]
    return ctx


def _safe_workspace_path(workspace: Path, rel: str) -> Path:
    path = (workspace / rel).resolve()
    if workspace.resolve() not in path.parents and workspace.resolve() != path:
        raise HTTPException(status_code=400, detail="invalid path")
    return path


def _safe_artifact_path(problem: str, build_id: str, rel: str) -> Path:
    root = (settings.artifacts_root / problem / build_id).resolve()
    path = (root / rel).resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found")
    return path


def _audit(actor_user_id: int, problem_id: int, action: str, details: dict) -> None:
    db.execute(
        "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
        [actor_user_id, problem_id, action, json.dumps(details), now_iso()],
    )


@app.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse("/problems/sample/alice/files")


@app.post("/switch-workspace")
def switch_workspace(problem: str = Form(...), user: str = Form(...), page: str = Form("files")):
    workspace_service.ensure_problem(problem, f"{problem.title()} Problem")
    workspace_service.ensure_workspace(problem, user)
    return RedirectResponse(f"/problems/{problem}/{user}/{page}", status_code=303)


@app.post("/switch-branch")
def switch_branch(problem: str = Form(...), user: str = Form(...), branch: str = Form(...), page: str = Form("files")):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        with workspace_service.workspace_lock(workspace):
            git_service.switch_branch(workspace, branch, create=False)
        _audit(ctx["user"]["id"], ctx["problem"]["id"], "git.switch", {"branch": branch, "create": False})
        return RedirectResponse(f"/problems/{problem}/{user}/{page}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/problems/{problem}/{user}/git?message={quote_plus(str(exc))}", status_code=303)


@app.get("/problems/{problem}/{user}/files", response_class=HTMLResponse)
def files_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    selected = request.query_params.get("path", "README.problem.md")
    selected_line = int(request.query_params.get("line", "1"))
    content = ""
    try:
        content = git_service.read_file(workspace, selected)
    except Exception:
        selected = "README.problem.md"
        content = git_service.read_file(workspace, selected)
    files = git_service.list_files(workspace)
    return templates.TemplateResponse(
        request,
        "files.html",
        {
            "ctx": ctx,
            "files": files,
            "selected": selected,
            "content": content,
            "selected_line": selected_line,
            "message": request.query_params.get("message", ""),
        },
    )


@app.post("/problems/{problem}/{user}/files/save")
def files_save(problem: str, user: str, path: str = Form(...), content: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.write_file(workspace, path, content)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.save", {"path": path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(path)}&message=saved", status_code=303)


@app.post("/problems/{problem}/{user}/files/new")
def files_new(problem: str, user: str, path: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.write_file(workspace, path, "")
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.new", {"path": path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(path)}&message=created", status_code=303)


@app.post("/problems/{problem}/{user}/files/upload")
async def files_upload(problem: str, user: str, path: str = Form(...), upload: UploadFile = File(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    payload = await upload.read()
    with workspace_service.workspace_lock(workspace):
        abs_path = _safe_workspace_path(workspace, path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(payload)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.upload", {"path": path, "bytes": len(payload)})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(path)}&message=uploaded", status_code=303)


@app.post("/problems/{problem}/{user}/files/rename")
def files_rename(problem: str, user: str, old_path: str = Form(...), new_path: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.rename_path(workspace, old_path, new_path)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.rename", {"old": old_path, "new": new_path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(new_path)}&message=renamed", status_code=303)


@app.post("/problems/{problem}/{user}/files/delete")
def files_delete(problem: str, user: str, path: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.delete_path(workspace, path)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.delete", {"path": path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?message=deleted", status_code=303)


@app.get("/problems/{problem}/{user}/files/download")
def files_download(problem: str, user: str, path: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    file_path = _safe_workspace_path(workspace, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(file_path, filename=file_path.name)


@app.get("/problems/{problem}/{user}/git", response_class=HTMLResponse)
def git_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    status = git_service.status(workspace)
    message = request.query_params.get("message", "")
    return templates.TemplateResponse(
        request,
        "git.html",
        {"ctx": ctx, "status": status, "branches": ctx.get("branches", []), "message": message},
    )


@app.post("/problems/{problem}/{user}/git/commit")
def git_commit(problem: str, user: str, message: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        with workspace_service.workspace_lock(workspace):
            head = git_service.commit(workspace, message, user, f"{user}@polygonlike.local")
        _audit(ctx["user"]["id"], ctx["problem"]["id"], "git.commit", {"message": message, "head": head})
        msg = "commit ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={quote_plus(msg)}", status_code=303)


@app.post("/problems/{problem}/{user}/git/push")
def git_push(problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    branch = ctx["workspace"]["branch"] or "main"
    try:
        with workspace_service.workspace_lock(workspace):
            git_service.push(workspace, branch)
        _audit(ctx["user"]["id"], ctx["problem"]["id"], "git.push", {"branch": branch})
        msg = "push ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={quote_plus(msg)}", status_code=303)


@app.post("/problems/{problem}/{user}/git/pull")
def git_pull(problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    branch = ctx["workspace"]["branch"] or "main"
    try:
        with workspace_service.workspace_lock(workspace):
            git_service.pull(workspace, branch)
        _audit(ctx["user"]["id"], ctx["problem"]["id"], "git.pull", {"branch": branch})
        msg = "pull ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={quote_plus(msg)}", status_code=303)


@app.post("/problems/{problem}/{user}/git/switch")
def git_switch(problem: str, user: str, branch: str = Form(...), create: str = Form("0")):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        with workspace_service.workspace_lock(workspace):
            git_service.switch_branch(workspace, branch, create == "1")
        _audit(ctx["user"]["id"], ctx["problem"]["id"], "git.switch", {"branch": branch, "create": create == "1"})
        msg = "switch ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={quote_plus(msg)}", status_code=303)


@app.post("/problems/{problem}/{user}/git/merge")
def git_merge(problem: str, user: str, source_branch: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        with workspace_service.workspace_lock(workspace):
            git_service.merge(workspace, source_branch, "main")
        _audit(ctx["user"]["id"], ctx["problem"]["id"], "git.merge", {"source_branch": source_branch, "target": "main"})
        msg = "merge ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={quote_plus(msg)}", status_code=303)


@app.get("/problems/{problem}/{user}/build", response_class=HTMLResponse)
def build_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    builds = db.fetch_all("SELECT * FROM builds WHERE problem_id=? ORDER BY created_at DESC LIMIT 30", [ctx["problem"]["id"]])
    selected = request.query_params.get("build_id")
    detail = db.fetch_one("SELECT * FROM builds WHERE id=?", [selected]) if selected else None
    logs = []
    summary = None
    diagnostics = []
    if detail:
        root = Path(detail["artifact_path"]) / "logs"
        if root.exists():
            for p in sorted(root.glob("*.log")):
                logs.append({"name": p.name, "content": p.read_text(encoding="utf-8")})
        if detail["summary_json"]:
            summary = json.loads(detail["summary_json"])
            diagnostics = summary.get("diagnostics", [])
    return templates.TemplateResponse(
        request,
        "build.html",
        {"ctx": ctx, "builds": builds, "detail": detail, "logs": logs, "summary": summary, "diagnostics": diagnostics},
    )


@app.post("/problems/{problem}/{user}/build/run")
def build_run(problem: str, user: str, commit: str = Form("")):
    build_id = build_service.run_build(problem, user, commit=commit or None)
    ctx = page_ctx(problem, user)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "build.run", {"build_id": build_id, "commit": commit or "HEAD"})
    return RedirectResponse(f"/problems/{problem}/{user}/build?build_id={build_id}", status_code=303)


@app.get("/problems/{problem}/{user}/preview", response_class=HTMLResponse)
def preview_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    preview_id = request.query_params.get("preview_id", "")
    previews = db.fetch_all(
        "SELECT id,status,source_commit,source_ref,created_at,finished_at FROM previews WHERE problem_id=? ORDER BY created_at DESC LIMIT 30",
        [ctx["problem"]["id"]],
    )
    log = ""
    pdf_exists = False
    log_refs = []
    if preview_id:
        root = settings.artifacts_root / problem / preview_id
        lp = root / "logs" / "latex.log"
        pdf = root / "statement_preview" / "statement.pdf"
        pdf_exists = pdf.exists()
        if lp.exists():
            log = lp.read_text(encoding="utf-8")
            tex_ref = re.compile(r"(?P<file>[\\w./-]+\\.tex):(?P<line>\\d+)")
            for line in log.splitlines():
                m = tex_ref.search(line)
                if m:
                    log_refs.append({"file": m.group("file"), "line": int(m.group("line")), "context": line})
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "ctx": ctx,
            "preview_id": preview_id,
            "previews": previews,
            "log": log,
            "pdf_exists": pdf_exists,
            "log_refs": log_refs,
        },
    )


@app.post("/problems/{problem}/{user}/preview/run")
def preview_run(problem: str, user: str, commit: str = Form("")):
    preview_id = preview_service.compile_preview(problem, user, commit=commit or None)
    ctx = page_ctx(problem, user)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "preview.run", {"preview_id": preview_id, "commit": commit or "HEAD"})
    return RedirectResponse(f"/problems/{problem}/{user}/preview?preview_id={preview_id}", status_code=303)


@app.get("/problems/{problem}/{user}/run", response_class=HTMLResponse)
def run_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    builds = db.fetch_all("SELECT id,status,created_at FROM builds WHERE problem_id=? ORDER BY created_at DESC", [ctx["problem"]["id"]])
    runs = db.fetch_all("SELECT * FROM runs WHERE problem_id=? ORDER BY created_at DESC LIMIT 30", [ctx["problem"]["id"]])
    detail_id = request.query_params.get("run_id", "")
    detail = db.fetch_one("SELECT * FROM runs WHERE id=?", [detail_id]) if detail_id else None
    summary = json.loads(detail["summary_json"]) if detail and detail["summary_json"] else None
    return templates.TemplateResponse(
        request,
        "run.html",
        {"ctx": ctx, "builds": builds, "runs": runs, "detail": detail, "summary": summary},
    )


@app.post("/problems/{problem}/{user}/run/execute")
def run_execute(
    problem: str,
    user: str,
    build_id: str = Form(...),
    submission_path: str = Form(""),
    mode: str = Form("pass-fail"),
    submission_upload: UploadFile | None = File(None),
):
    upload_content = None
    upload_filename = None
    if submission_upload is not None:
        upload_content = submission_upload.file.read()
        upload_filename = submission_upload.filename or None
        if upload_content == b"":
            upload_content = None

    run_id = run_service.run_submission(
        problem,
        user,
        build_id,
        submission_path=submission_path or None,
        mode=mode,
        upload_content=upload_content,
        upload_filename=upload_filename,
    )
    ctx = page_ctx(problem, user)
    _audit(
        ctx["user"]["id"],
        ctx["problem"]["id"],
        "run.execute",
        {
            "run_id": run_id,
            "build_id": build_id,
            "submission_path": submission_path or None,
            "uploaded": bool(upload_content),
            "mode": mode,
        },
    )
    return RedirectResponse(f"/problems/{problem}/{user}/run?run_id={run_id}", status_code=303)


@app.get("/problems/{problem}/{user}/export", response_class=HTMLResponse)
def export_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    builds = db.fetch_all("SELECT id,status,created_at FROM builds WHERE problem_id=? ORDER BY created_at DESC", [ctx["problem"]["id"]])
    exports = db.fetch_all(
        "SELECT * FROM exports WHERE problem_id=? ORDER BY created_at DESC LIMIT 40",
        [ctx["problem"]["id"]],
    )
    message = request.query_params.get("message", "")
    return templates.TemplateResponse(request, "export.html", {"ctx": ctx, "builds": builds, "exports": exports, "message": message})


@app.post("/problems/{problem}/{user}/export/create")
def export_create(problem: str, user: str, build_id: str = Form(...), export_type: str = Form(...)):
    ctx = page_ctx(problem, user)
    try:
        out = export_service.create_export(problem, build_id, export_type)
        _audit(
            ctx["user"]["id"],
            ctx["problem"]["id"],
            "export.create",
            {"build_id": build_id, "export_type": export_type, "filename": out.name},
        )
        msg = f"created {out.name}"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/export?message={quote_plus(msg)}", status_code=303)


@app.get("/problems/{problem}/{user}/artifacts/{build_id}/{rel_path:path}")
def artifact_file(problem: str, user: str, build_id: str, rel_path: str):
    _ = page_ctx(problem, user)
    file_path = _safe_artifact_path(problem, build_id, rel_path)
    return FileResponse(file_path, filename=file_path.name)


@app.get("/problems/{problem}/{user}/artifacts/{build_id}/browse", response_class=HTMLResponse)
def artifact_browse(request: Request, problem: str, user: str, build_id: str, rel: str = "tests"):
    ctx = page_ctx(problem, user)
    root = (settings.artifacts_root / problem / build_id).resolve()
    target = (root / rel).resolve()
    if root not in target.parents and root != target:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="artifact directory not found")
    files = sorted([str(p.relative_to(root)) for p in target.rglob("*") if p.is_file()])
    return templates.TemplateResponse(
        request,
        "artifact_browser.html",
        {"ctx": ctx, "build_id": build_id, "rel": rel, "files": files},
    )


@app.get("/api/problems")
def api_problems():
    return [dict(r) for r in db.fetch_all("SELECT * FROM problems ORDER BY created_at DESC")]


@app.get("/api/problems/{problem}/workspaces/{user}/status")
def api_workspace_status(problem: str, user: str):
    ctx = page_ctx(problem, user)
    return {
        "problem": ctx["problem"]["slug"],
        "workspace": ctx["workspace"]["path"],
        "branch": ctx["workspace"]["branch"],
        "head": ctx["workspace"]["head_commit"],
        "dirty": bool(ctx["workspace"]["dirty"]),
        "recent_build": ctx["latest_build"],
        "recent_preview": ctx.get("latest_preview"),
    }


@app.get("/api/problems/{problem}/workspaces/{user}/branches")
def api_workspace_branches(problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    return {"branches": git_service.list_branches(workspace), "current": ctx["workspace"]["branch"]}


@app.get("/api/problems/{problem}/workspaces/{user}/recent-builds")
def api_recent_builds(problem: str, user: str):
    ctx = page_ctx(problem, user)
    rows = db.fetch_all(
        "SELECT id,status,source_commit,source_ref,created_at,finished_at FROM builds WHERE problem_id=? ORDER BY created_at DESC LIMIT 20",
        [ctx["problem"]["id"]],
    )
    return [dict(r) for r in rows]


@app.get("/api/problems/{problem}/workspaces/{user}/recent-previews")
def api_recent_previews(problem: str, user: str):
    ctx = page_ctx(problem, user)
    rows = db.fetch_all(
        "SELECT id,status,source_commit,source_ref,created_at,finished_at FROM previews WHERE problem_id=? ORDER BY created_at DESC LIMIT 20",
        [ctx["problem"]["id"]],
    )
    return [dict(r) for r in rows]


@app.get("/api/problems/{problem}/builds/{build_id}/manifest")
def api_manifest(problem: str, build_id: str):
    p = settings.artifacts_root / problem / build_id / "manifest.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="manifest not found")
    return json.loads(p.read_text(encoding="utf-8"))
