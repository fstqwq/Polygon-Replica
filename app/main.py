from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

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
run_service = RunService(db, workspace_service, toolchain_service)
export_service = ExportService(db, settings.artifacts_root)

app = FastAPI(title="Polygonlike")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup() -> None:
    db.init()
    workspace_service.ensure_problem("sample", "Sample Problem")
    workspace_service.ensure_workspace("sample", "alice")


def page_ctx(problem: str, user: str, include_branches: bool = True, refresh_status: bool = True) -> dict:
    try:
        workspace_service.ensure_workspace(problem, user, refresh_status=refresh_status)
        ctx = workspace_service.workspace_context(problem, user)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if include_branches:
        workspace = Path(ctx["workspace"]["path"])
        try:
            ctx["branches"] = git_service.list_branches(workspace)
        except Exception:
            ctx["branches"] = [ctx["workspace"].get("branch") or "main"]
    else:
        ctx["branches"] = [ctx["workspace"].get("branch") or "main"]
    return ctx


def _safe_workspace_path(workspace: Path, rel: str) -> Path:
    path = (workspace / rel).resolve()
    if workspace.resolve() not in path.parents and workspace.resolve() != path:
        raise HTTPException(status_code=400, detail="invalid path")
    return path


def _artifact_root(problem: str, artifact_id: str) -> Path:
    aid = str(artifact_id or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", aid):
        raise HTTPException(status_code=404, detail="artifact not found")
    base = (settings.artifacts_root / problem).resolve()
    root = (base / aid).resolve()
    try:
        rel = root.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=404, detail="artifact not found")
    if len(rel.parts) != 1 or rel.parts[0] != aid:
        raise HTTPException(status_code=404, detail="artifact not found")
    return root


def _safe_artifact_path(problem: str, build_id: str, rel: str) -> Path:
    root = _artifact_root(problem, build_id)
    path = (root / rel).resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found")
    return path


def _safe_artifact_dir(problem: str, build_id: str, rel: str) -> tuple[Path, Path]:
    root = _artifact_root(problem, build_id)
    path = (root / rel).resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="artifact directory not found")
    return root, path


def _safe_descendant_files(root: Path, target: Path) -> list[Path]:
    root_resolved = root.resolve()
    safe_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        dir_root = Path(dirpath)
        pruned_dirs: list[str] = []
        for name in dirnames:
            d = dir_root / name
            if d.is_symlink():
                continue
            try:
                resolved = d.resolve()
            except OSError:
                continue
            if root_resolved in resolved.parents or root_resolved == resolved:
                pruned_dirs.append(name)
        dirnames[:] = pruned_dirs

        for name in filenames:
            p = dir_root / name
            if p.is_symlink():
                continue
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if root_resolved not in resolved.parents and root_resolved != resolved:
                continue
            safe_files.append(p)
    safe_files.sort(key=lambda p: str(p.relative_to(root)))
    return safe_files


def _workspace_run_artifact_root(ctx: dict, run_id: str) -> Path:
    row = db.fetch_one(
        "SELECT artifact_path FROM runs WHERE id=? AND problem_id=? AND workspace_id=?",
        [run_id, ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="run not found in workspace")
    root = Path(str(row["artifact_path"] or "")).resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail="run artifact directory not found")

    valid = False
    artifacts_problem_root = (settings.artifacts_root / ctx["problem"]["slug"]).resolve()
    invalid_runs_root = (settings.run_root / "invalid-runs").resolve()
    try:
        rel = root.relative_to(artifacts_problem_root)
        if (
            len(rel.parts) == 3
            and rel.parts[1] == "logs"
            and rel.parts[2] == f"run-{run_id}"
            and re.fullmatch(r"[A-Za-z0-9._-]+", rel.parts[0])
        ):
            valid = True
    except ValueError:
        pass
    if root == (invalid_runs_root / run_id).resolve():
        valid = True

    if not valid:
        raise HTTPException(status_code=404, detail="run artifact directory not found")
    return root


def _normalize_run_artifact_rel(root: Path, run_id: str, rel: str) -> str:
    candidate = rel.lstrip("/")
    legacy_prefix = f"logs/run-{run_id}/"
    if candidate.startswith(legacy_prefix):
        maybe = candidate[len(legacy_prefix) :]
        probe = (root / maybe).resolve()
        if root in probe.parents or root == probe:
            return maybe
    return candidate


def _safe_run_artifact_path(ctx: dict, run_id: str, rel: str) -> Path:
    root = _workspace_run_artifact_root(ctx, run_id)
    norm_rel = _normalize_run_artifact_rel(root, run_id, rel)
    path = (root / norm_rel).resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail="invalid run artifact path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="run artifact file not found")
    return path


def _safe_run_artifact_dir(ctx: dict, run_id: str, rel: str) -> tuple[Path, Path]:
    root = _workspace_run_artifact_root(ctx, run_id)
    norm_rel = _normalize_run_artifact_rel(root, run_id, rel)
    path = (root / norm_rel).resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail="invalid run artifact path")
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="run artifact directory not found")
    return root, path


def _assert_workspace_build_access(ctx: dict, build_id: str) -> None:
    row = db.fetch_one(
        "SELECT id FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
        [build_id, ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="build not found in workspace")


def _assert_workspace_artifact_access(ctx: dict, artifact_id: str) -> None:
    params = [artifact_id, ctx["problem"]["id"], ctx["workspace"]["id"]]
    build_row = db.fetch_one(
        "SELECT id FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
        params,
    )
    if build_row is not None:
        return
    preview_row = db.fetch_one(
        "SELECT id FROM previews WHERE id=? AND problem_id=? AND workspace_id=?",
        params,
    )
    if preview_row is not None:
        return
    raise HTTPException(status_code=404, detail="artifact not found in workspace")


def _audit(actor_user_id: int, problem_id: int, action: str, details: dict) -> None:
    db.execute(
        "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
        [actor_user_id, problem_id, action, json.dumps(details), now_iso()],
    )


def _normalize_page_target(page: str) -> str:
    raw = str(page or "").strip().lower()
    aliases = {"artifacts": "build", "runs": "run"}
    normalized = aliases.get(raw, raw)
    allowed = {"files", "git", "build", "preview", "run", "export"}
    return normalized if normalized in allowed else "files"


def _parse_summary_json(raw: str | None, label: str) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return {"error": f"invalid summary_json for {label}"}
    if isinstance(payload, dict):
        return payload
    return {"error": f"summary_json for {label} must be a JSON object"}


def _read_text_safe(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_line_param(raw: str | None, default: int = 1) -> int:
    try:
        line = int(str(raw or "").strip())
    except Exception:
        return default
    return line if line > 0 else default


@app.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse("/problems/sample/alice/files")


@app.post("/switch-workspace")
def switch_workspace(problem: str = Form(...), user: str = Form(...), page: str = Form("files")):
    workspace_service.ensure_problem(problem, f"{problem.title()} Problem")
    workspace_service.ensure_workspace(problem, user)
    target_page = _normalize_page_target(page)
    return RedirectResponse(f"/problems/{problem}/{user}/{target_page}", status_code=303)


@app.post("/switch-branch")
def switch_branch(problem: str = Form(...), user: str = Form(...), branch: str = Form(...), page: str = Form("files")):
    target_page = _normalize_page_target(page)
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    try:
        with workspace_service.workspace_lock(workspace):
            git_service.switch_branch(workspace, branch, create=False)
        _audit(ctx["user"]["id"], ctx["problem"]["id"], "git.switch", {"branch": branch, "create": False})
        return RedirectResponse(f"/problems/{problem}/{user}/{target_page}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/problems/{problem}/{user}/git?message={quote_plus(str(exc))}", status_code=303)


@app.get("/problems/{problem}/{user}/files", response_class=HTMLResponse)
def files_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx["workspace"]["path"])
    selected = request.query_params.get("path", "README.problem.md")
    selected_line = _parse_line_param(request.query_params.get("line", "1"))
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.write_file(workspace, path, content)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.save", {"path": path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(path)}&message=saved", status_code=303)


@app.post("/problems/{problem}/{user}/files/new")
def files_new(problem: str, user: str, path: str = Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.write_file(workspace, path, "")
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.new", {"path": path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(path)}&message=created", status_code=303)


@app.post("/problems/{problem}/{user}/files/upload")
async def files_upload(problem: str, user: str, path: str = Form(...), upload: UploadFile = File(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    workspace = Path(ctx["workspace"]["path"])
    total_bytes = 0
    with workspace_service.workspace_lock(workspace):
        abs_path = _safe_workspace_path(workspace, path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with abs_path.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                total_bytes += len(chunk)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.upload", {"path": path, "bytes": total_bytes})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(path)}&message=uploaded", status_code=303)


@app.post("/problems/{problem}/{user}/files/rename")
def files_rename(problem: str, user: str, old_path: str = Form(...), new_path: str = Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.rename_path(workspace, old_path, new_path)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.rename", {"old": old_path, "new": new_path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?path={quote_plus(new_path)}&message=renamed", status_code=303)


@app.post("/problems/{problem}/{user}/files/delete")
def files_delete(problem: str, user: str, path: str = Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    workspace = Path(ctx["workspace"]["path"])
    with workspace_service.workspace_lock(workspace):
        git_service.delete_path(workspace, path)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "files.delete", {"path": path})
    return RedirectResponse(f"/problems/{problem}/{user}/files?message=deleted", status_code=303)


@app.get("/problems/{problem}/{user}/files/download")
def files_download(problem: str, user: str, path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
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
    workspace_id = ctx["workspace"]["id"]
    builds = db.fetch_all(
        "SELECT * FROM builds WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 30",
        [ctx["problem"]["id"], workspace_id],
    )
    selected = request.query_params.get("build_id")
    detail = (
        db.fetch_one("SELECT * FROM builds WHERE id=? AND workspace_id=?", [selected, workspace_id])
        if selected
        else None
    )
    logs = []
    summary = None
    diagnostics = []
    if detail:
        try:
            artifact_root = _artifact_root(problem, str(detail["id"]))
            logs_root = artifact_root / "logs"
            if logs_root.exists() and logs_root.is_dir():
                for p in sorted(logs_root.glob("*.log")):
                    if p.is_symlink() or not p.is_file():
                        continue
                    try:
                        resolved = p.resolve()
                    except OSError:
                        continue
                    if artifact_root not in resolved.parents and artifact_root != resolved:
                        continue
                    logs.append({"name": p.name, "content": _read_text_safe(p)})
        except HTTPException:
            logs = []
        summary = _parse_summary_json(detail["summary_json"], "build")
        if summary:
            maybe_diagnostics = summary.get("diagnostics", [])
            diagnostics = maybe_diagnostics if isinstance(maybe_diagnostics, list) else []
    return templates.TemplateResponse(
        request,
        "build.html",
        {"ctx": ctx, "builds": builds, "detail": detail, "logs": logs, "summary": summary, "diagnostics": diagnostics},
    )


@app.post("/problems/{problem}/{user}/build/run")
def build_run(problem: str, user: str, commit: str = Form("")):
    build_id = build_service.run_build(problem, user, commit=commit or None)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "build.run", {"build_id": build_id, "commit": commit or "HEAD"})
    return RedirectResponse(f"/problems/{problem}/{user}/build?build_id={build_id}", status_code=303)


@app.get("/problems/{problem}/{user}/preview", response_class=HTMLResponse)
def preview_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx["workspace"]["id"]
    preview_id = request.query_params.get("preview_id", "")
    previews = db.fetch_all(
        "SELECT id,status,source_commit,source_ref,created_at,finished_at FROM previews WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 30",
        [ctx["problem"]["id"], workspace_id],
    )
    log = ""
    pdf_exists = False
    log_refs = []
    if preview_id:
        preview_row = db.fetch_one(
            "SELECT id FROM previews WHERE id=? AND problem_id=? AND workspace_id=?",
            [preview_id, ctx["problem"]["id"], workspace_id],
        )
        if preview_row is None:
            preview_id = ""
    if preview_id:
        try:
            root = _artifact_root(problem, preview_id)
        except HTTPException:
            preview_id = ""
        else:
            lp = root / "logs" / "latex.log"
            pdf = root / "statement_preview" / "statement.pdf"
            pdf_exists = pdf.exists()
            if lp.exists():
                log = _read_text_safe(lp)
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    _audit(ctx["user"]["id"], ctx["problem"]["id"], "preview.run", {"preview_id": preview_id, "commit": commit or "HEAD"})
    return RedirectResponse(f"/problems/{problem}/{user}/preview?preview_id={preview_id}", status_code=303)


@app.get("/problems/{problem}/{user}/run", response_class=HTMLResponse)
def run_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx["workspace"]["id"]
    builds = db.fetch_all(
        "SELECT id,status,created_at FROM builds WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC",
        [ctx["problem"]["id"], workspace_id],
    )
    runs = db.fetch_all(
        "SELECT * FROM runs WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 30",
        [ctx["problem"]["id"], workspace_id],
    )
    detail_id = request.query_params.get("run_id", "")
    detail = db.fetch_one("SELECT * FROM runs WHERE id=? AND workspace_id=?", [detail_id, workspace_id]) if detail_id else None
    summary = _parse_summary_json(detail["summary_json"], "run") if detail else None
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
    upload_stream = None
    upload_filename = None
    uploaded = False
    if submission_upload is not None:
        upload_stream = submission_upload.file
        upload_filename = submission_upload.filename or None
        uploaded = True
    try:
        run_id = run_service.run_submission(
            problem,
            user,
            build_id,
            submission_path=submission_path or None,
            mode=mode,
            upload_stream=upload_stream,
            upload_filename=upload_filename,
        )
    finally:
        if submission_upload is not None:
            submission_upload.file.close()
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    _audit(
        ctx["user"]["id"],
        ctx["problem"]["id"],
        "run.execute",
        {
            "run_id": run_id,
            "build_id": build_id,
            "submission_path": submission_path or None,
            "uploaded": uploaded,
            "mode": mode,
        },
    )
    return RedirectResponse(f"/problems/{problem}/{user}/run?run_id={run_id}", status_code=303)


@app.get("/problems/{problem}/{user}/export", response_class=HTMLResponse)
def export_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx["workspace"]["id"]
    builds = db.fetch_all(
        "SELECT id,status,created_at FROM builds WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC",
        [ctx["problem"]["id"], workspace_id],
    )
    exports = db.fetch_all(
        """
        SELECT e.*
        FROM exports e
        JOIN builds b ON b.id = e.build_id
        WHERE e.problem_id=? AND b.workspace_id=?
        ORDER BY e.created_at DESC
        LIMIT 40
        """,
        [ctx["problem"]["id"], workspace_id],
    )
    message = request.query_params.get("message", "")
    return templates.TemplateResponse(request, "export.html", {"ctx": ctx, "builds": builds, "exports": exports, "message": message})


@app.post("/problems/{problem}/{user}/export/create")
def export_create(problem: str, user: str, build_id: str = Form(...), export_type: str = Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    try:
        _assert_workspace_build_access(ctx, build_id)
        out = export_service.create_export(problem, build_id, export_type)
        _audit(
            ctx["user"]["id"],
            ctx["problem"]["id"],
            "export.create",
            {"build_id": build_id, "export_type": export_type, "filename": out.name},
        )
        msg = f"created {out.name}"
    except HTTPException as exc:
        msg = str(exc.detail)
    except Exception as exc:
        msg = str(exc)
    return RedirectResponse(f"/problems/{problem}/{user}/export?message={quote_plus(msg)}", status_code=303)


@app.get("/problems/{problem}/{user}/artifacts/{build_id}/download-dir")
def artifact_download_dir(problem: str, user: str, build_id: str, rel: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    _assert_workspace_artifact_access(ctx, build_id)
    root, target = _safe_artifact_dir(problem, build_id, rel)
    fd, tmp_zip = tempfile.mkstemp(prefix=f"{build_id}-", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_zip)
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in _safe_descendant_files(root, target):
            zf.write(p, arcname=str(p.relative_to(root)))
    name = f"{Path(rel).name or 'artifacts'}-{build_id}.zip"
    return FileResponse(
        tmp_path,
        filename=name,
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )


@app.get("/problems/{problem}/{user}/artifacts/{build_id}/browse", response_class=HTMLResponse)
def artifact_browse(request: Request, problem: str, user: str, build_id: str, rel: str = "tests"):
    ctx = page_ctx(problem, user)
    _assert_workspace_artifact_access(ctx, build_id)
    root, target = _safe_artifact_dir(problem, build_id, rel)
    files = [str(p.relative_to(root)) for p in _safe_descendant_files(root, target)]
    return templates.TemplateResponse(
        request,
        "artifact_browser.html",
        {"ctx": ctx, "build_id": build_id, "rel": rel, "files": files},
    )


@app.get("/problems/{problem}/{user}/artifacts/{build_id}/{rel_path:path}")
def artifact_file(problem: str, user: str, build_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    _assert_workspace_artifact_access(ctx, build_id)
    file_path = _safe_artifact_path(problem, build_id, rel_path)
    return FileResponse(file_path, filename=file_path.name)


@app.get("/problems/{problem}/{user}/runs/{run_id}/download-dir")
def run_artifact_download_dir(problem: str, user: str, run_id: str, rel: str = "feedback_dir"):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    root, target = _safe_run_artifact_dir(ctx, run_id, rel)
    fd, tmp_zip = tempfile.mkstemp(prefix=f"{run_id}-", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_zip)
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in _safe_descendant_files(root, target):
            zf.write(p, arcname=str(p.relative_to(root)))
    name = f"{Path(rel).name or 'run-artifacts'}-{run_id}.zip"
    return FileResponse(
        tmp_path,
        filename=name,
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )


@app.get("/problems/{problem}/{user}/runs/{run_id}/browse", response_class=HTMLResponse)
def run_artifact_browse(request: Request, problem: str, user: str, run_id: str, rel: str = "feedback_dir"):
    ctx = page_ctx(problem, user)
    root, target = _safe_run_artifact_dir(ctx, run_id, rel)
    files = [str(p.relative_to(root)) for p in _safe_descendant_files(root, target)]
    return templates.TemplateResponse(
        request,
        "run_artifact_browser.html",
        {"ctx": ctx, "run_id": run_id, "rel": rel, "files": files},
    )


@app.get("/problems/{problem}/{user}/runs/{run_id}/artifacts/{rel_path:path}")
def run_artifact_file(problem: str, user: str, run_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    file_path = _safe_run_artifact_path(ctx, run_id, rel_path)
    return FileResponse(file_path, filename=file_path.name)


@app.get("/api/problems")
def api_problems():
    return [dict(r) for r in db.fetch_all("SELECT * FROM problems ORDER BY created_at DESC")]


@app.get("/api/problems/{problem}/workspaces/{user}/status")
def api_workspace_status(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True)
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
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True)
    workspace = Path(ctx["workspace"]["path"])
    try:
        branches = git_service.list_branches(workspace)
    except Exception:
        branches = [ctx["workspace"].get("branch") or "main"]
    return {"branches": branches, "current": ctx["workspace"]["branch"]}


@app.get("/api/problems/{problem}/workspaces/{user}/recent-builds")
def api_recent_builds(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    rows = db.fetch_all(
        "SELECT id,status,source_commit,source_ref,created_at,finished_at FROM builds WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 20",
        [ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    return [dict(r) for r in rows]


@app.get("/api/problems/{problem}/workspaces/{user}/recent-previews")
def api_recent_previews(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    rows = db.fetch_all(
        "SELECT id,status,source_commit,source_ref,created_at,finished_at FROM previews WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 20",
        [ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    return [dict(r) for r in rows]


@app.get("/api/problems/{problem}/workspaces/{user}/recent-runs")
def api_recent_runs(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    rows = db.fetch_all(
        "SELECT id,build_id,mode,status,created_at,finished_at FROM runs WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 20",
        [ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    return [dict(r) for r in rows]


@app.get("/api/problems/{problem}/workspaces/{user}/recent-exports")
def api_recent_exports(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    rows = db.fetch_all(
        """
        SELECT e.id,e.build_id,e.export_type,e.filename,e.size_bytes,e.sha256,e.source_commit,e.created_at
        FROM exports e
        JOIN builds b ON b.id = e.build_id
        WHERE e.problem_id=? AND b.workspace_id=?
        ORDER BY e.created_at DESC
        LIMIT 20
        """,
        [ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    return [dict(r) for r in rows]


@app.get("/api/problems/{problem}/workspaces/{user}/builds/{build_id}/manifest")
def api_workspace_manifest(problem: str, user: str, build_id: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False)
    row = db.fetch_one(
        "SELECT id FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
        [build_id, ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="manifest not found in workspace")
    p = _artifact_root(problem, build_id) / "manifest.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="manifest not found")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/problems/{problem}/builds/{build_id}/manifest")
def api_manifest(problem: str, build_id: str, user: str | None = None):
    if not user:
        raise HTTPException(status_code=400, detail="use workspace manifest endpoint")
    return api_workspace_manifest(problem, user, build_id)
