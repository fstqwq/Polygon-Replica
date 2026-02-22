from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import DB
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
    return workspace_service.workspace_context(problem, user)


@app.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse("/problems/sample/alice/files")


@app.get("/problems/{problem}/{user}/files", response_class=HTMLResponse)
def files_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    selected = request.query_params.get("path", "README.problem.md")
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
        {"ctx": ctx, "files": files, "selected": selected, "content": content},
    )


@app.post("/problems/{problem}/{user}/files/save")
def files_save(problem: str, user: str, path: str = Form(...), content: str = Form(...)):
    ctx = page_ctx(problem, user)
    git_service.write_file(Path(ctx["workspace"]["path"]), path, content)
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={path}", status_code=303)


@app.post("/problems/{problem}/{user}/files/new")
def files_new(problem: str, user: str, path: str = Form(...)):
    ctx = page_ctx(problem, user)
    git_service.write_file(Path(ctx["workspace"]["path"]), path, "")
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={path}", status_code=303)


@app.post("/problems/{problem}/{user}/files/delete")
def files_delete(problem: str, user: str, path: str = Form(...)):
    ctx = page_ctx(problem, user)
    git_service.delete_path(Path(ctx["workspace"]["path"]), path)
    return RedirectResponse(f"/problems/{problem}/{user}/files", status_code=303)


@app.get("/problems/{problem}/{user}/git", response_class=HTMLResponse)
def git_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    status = git_service.status(Path(ctx["workspace"]["path"]))
    message = request.query_params.get("message", "")
    return templates.TemplateResponse(request, "git.html", {"ctx": ctx, "status": status, "message": message})


@app.post("/problems/{problem}/{user}/git/commit")
def git_commit(problem: str, user: str, message: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        git_service.commit(workspace, message, user, f"{user}@polygonlike.local")
        msg = "commit ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={msg}", status_code=303)


@app.post("/problems/{problem}/{user}/git/push")
def git_push(problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        git_service.push(workspace, ctx["workspace"]["branch"] or "main")
        msg = "push ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={msg}", status_code=303)


@app.post("/problems/{problem}/{user}/git/pull")
def git_pull(problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        git_service.pull(workspace, ctx["workspace"]["branch"] or "main")
        msg = "pull ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={msg}", status_code=303)


@app.post("/problems/{problem}/{user}/git/switch")
def git_switch(problem: str, user: str, branch: str = Form(...), create: str = Form("0")):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        git_service.switch_branch(workspace, branch, create == "1")
        msg = "switch ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={msg}", status_code=303)


@app.post("/problems/{problem}/{user}/git/merge")
def git_merge(problem: str, user: str, source_branch: str = Form(...)):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        git_service.merge(workspace, source_branch, "main")
        msg = "merge ok"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/git?message={msg}", status_code=303)


@app.get("/problems/{problem}/{user}/build", response_class=HTMLResponse)
def build_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    builds = db.fetch_all("SELECT * FROM builds WHERE problem_id=? ORDER BY created_at DESC LIMIT 20", [ctx["problem"]["id"]])
    selected = request.query_params.get("build_id")
    detail = db.fetch_one("SELECT * FROM builds WHERE id=?", [selected]) if selected else None
    logs = []
    if detail:
        root = Path(detail["artifact_path"]) / "logs"
        if root.exists():
            for p in sorted(root.glob("*.log")):
                logs.append({"name": p.name, "content": p.read_text(encoding="utf-8")})
    return templates.TemplateResponse(request, "build.html", {"ctx": ctx, "builds": builds, "detail": detail, "logs": logs})


@app.post("/problems/{problem}/{user}/build/run")
def build_run(problem: str, user: str, commit: str = Form("")):
    build_id = build_service.run_build(problem, user, commit=commit or None)
    return RedirectResponse(f"/problems/{problem}/{user}/build?build_id={build_id}", status_code=303)


@app.get("/problems/{problem}/{user}/preview", response_class=HTMLResponse)
def preview_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    preview_id = request.query_params.get("preview_id", "")
    log = ""
    pdf_exists = False
    if preview_id:
        root = settings.artifacts_root / problem / preview_id
        lp = root / "logs" / "latex.log"
        pdf = root / "statement_preview" / "main.pdf"
        if not pdf.exists():
            pdf = root / "statement_preview" / "statement.pdf"
        pdf_exists = pdf.exists()
        if lp.exists():
            log = lp.read_text(encoding="utf-8")
    return templates.TemplateResponse(request, "preview.html", {"ctx": ctx, "preview_id": preview_id, "log": log, "pdf_exists": pdf_exists})


@app.post("/problems/{problem}/{user}/preview/run")
def preview_run(problem: str, user: str, commit: str = Form("")):
    preview_id = preview_service.compile_preview(problem, user, commit=commit or None)
    return RedirectResponse(f"/problems/{problem}/{user}/preview?preview_id={preview_id}", status_code=303)


@app.get("/problems/{problem}/{user}/run", response_class=HTMLResponse)
def run_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    builds = db.fetch_all("SELECT id,status,created_at FROM builds WHERE problem_id=? ORDER BY created_at DESC", [ctx["problem"]["id"]])
    runs = db.fetch_all("SELECT * FROM runs WHERE problem_id=? ORDER BY created_at DESC LIMIT 20", [ctx["problem"]["id"]])
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
    submission_path: str = Form(...),
    mode: str = Form("pass-fail"),
):
    run_id = run_service.run_submission(problem, user, build_id, submission_path, mode=mode)
    return RedirectResponse(f"/problems/{problem}/{user}/run?run_id={run_id}", status_code=303)


@app.get("/problems/{problem}/{user}/export", response_class=HTMLResponse)
def export_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    builds = db.fetch_all("SELECT id,status,created_at FROM builds WHERE problem_id=? ORDER BY created_at DESC", [ctx["problem"]["id"]])
    exports = db.fetch_all(
        "SELECT * FROM exports WHERE problem_id=? ORDER BY created_at DESC LIMIT 30",
        [ctx["problem"]["id"]],
    )
    message = request.query_params.get("message", "")
    return templates.TemplateResponse(request, "export.html", {"ctx": ctx, "builds": builds, "exports": exports, "message": message})


@app.post("/problems/{problem}/{user}/export/create")
def export_create(problem: str, user: str, build_id: str = Form(...), export_type: str = Form(...)):
    try:
        out = export_service.create_export(problem, build_id, export_type)
        msg = f"created {out.name}"
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/export?message={msg}", status_code=303)


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
    }


@app.get("/api/problems/{problem}/builds/{build_id}/manifest")
def api_manifest(problem: str, build_id: str):
    p = settings.artifacts_root / problem / build_id / "manifest.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="manifest not found")
    return json.loads(p.read_text(encoding="utf-8"))
