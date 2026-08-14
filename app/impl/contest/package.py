from app.impl.auth.session import require_session_user
from typing import Annotated

from pathlib import Path
from urllib.parse import quote_plus, urlencode

from fastapi import File, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app.impl.auth.shared import template_response
from app.impl.runtime.dependency import runtime
from app.main_util import enforce_textarea_max_bytes, read_upload_bytes_limited
from app.service.statement.constant import DEFAULT_OLYMP_STY
from app.service.statement.context import normalize_statement_language

from app.impl.contest.shared import (
    _contest_ctx,
    _contest_redirect,
)


def _contest_packages_statement_query(
    *,
    language: str,
    source_path: str = "",
    job_id: str = "",
) -> str:
    params = {"language": normalize_statement_language(language) or "english"}
    if source_path:
        params["source_path"] = source_path
    if job_id:
        params["job_id"] = job_id
    return urlencode(params)


def _contest_statement_language(contest_id: int, language: str) -> str:
    return runtime().contest_statement_service.resolve_language(contest_id, language)


def _contest_statement_source_key(*, contest_id: int, language: str, path: str, default_filename: str = "statements.tex", upload_filename: str = "") -> str:
    return runtime().contest_service.normalize_statement_source_key(
        language=_contest_statement_language(int(contest_id), language),
        path=path,
        upload_filename=upload_filename,
        default_filename=default_filename,
    )


def _contest_statement_display_path(key: str, language: str) -> str:
    prefix = f"statements/{normalize_statement_language(language) or 'english'}/"
    safe_key = str(key or "").strip()
    if safe_key.startswith(prefix):
        return safe_key[len(prefix):]
    return safe_key


def _contest_statement_language_options(contest_id: int, current_language: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    def _append(raw_language: object) -> None:
        safe = normalize_statement_language(raw_language)
        if safe and safe not in seen:
            seen.add(safe)
            result.append(safe)

    for language in [
        current_language,
        runtime().contest_service.statement_default_language(int(contest_id)),
        "english",
    ]:
        _append(language)
    for attachment in runtime().contest_service.statement_attachment_rows(int(contest_id)):
        parts = Path(str(attachment.get("rel_path") or "")).parts
        if len(parts) >= 3 and parts[0] == "statements":
            _append(parts[1])
    problem_languages: set[str] = set()
    for problem in runtime().contest_service.contest_problems(int(contest_id)):
        problem_languages.update(
            runtime().workspace_service.committed_statement_languages(
                problem["problem_slug"]
            )
        )
    for language in sorted(problem_languages):
        _append(language)
    return result


def _contest_default_statement_source_text(contest_id: int, contest_slug: str, language: str, display_path: str) -> str:
    safe_display_path = str(display_path or "").strip()
    if safe_display_path == "olymp.sty":
        return DEFAULT_OLYMP_STY
    if safe_display_path != "statements.tex":
        return ""
    return runtime().contest_statement_service.default_statements_tex(
        contest_id=int(contest_id),
        contest_slug=contest_slug,
        language=language,
        problem_entries=runtime().contest_service.contest_problem_entries(
            int(contest_id)
        ),
        source_folder_map=runtime().contest_service.statement_problem_source_folders(int(contest_id)),
    )


def _contest_statement_source_rows(contest_id: int, contest_slug: str, language: str) -> list[dict[str, object]]:
    prefix = f"statements/{language}/"
    rows: list[dict[str, object]] = []
    for row in runtime().contest_service.statement_attachment_rows(int(contest_id)):
        key = str(row.get("rel_path") or "").strip()
        if not key.startswith(prefix):
            continue
        display_path = key[len(prefix):]
        size_bytes: int | None = None
        exists = False
        try:
            source_path = runtime().contest_service.statement_file_path(contest_slug, key)
            exists = source_path.exists() and source_path.is_file() and (not source_path.is_symlink())
            if exists:
                size_bytes = source_path.stat().st_size
        except OSError:
            exists = False
        rows.append(
            {
                "key": key,
                "display_path": display_path,
                "display_path_q": quote_plus(display_path),
                "is_text": runtime().contest_service.statement_source_is_text(key),
                "exists": exists,
                "size_bytes": size_bytes,
                "created_at": str(row.get("created_at") or ""),
                "download_href": f"/contests/{contest_slug}/packages/statement/files?{urlencode({'language': language, 'path': display_path})}",
                "edit_href": f"/contests/{contest_slug}/packages?{urlencode({'language': language, 'source_path': display_path})}",
            }
        )
    return sorted(rows, key=lambda item: str(item["display_path"]))


def contest_packages_build_start(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    outputs: list[str] = Form([]),
    language: Annotated[str, Form()] = "",
    insert_blank_pages: Annotated[bool, Form()] = False,
):
    ctx = _contest_ctx(contest, user, "packages", request=request)
    if not ctx["access"]["can_build"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["build_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    requested_outputs = tuple(outputs)
    current_language = _contest_statement_language(contest_id, language)
    if runtime().contest_service.problem_count(contest_id) <= 0:
        return _contest_redirect(str(ctx["contest"]["slug"]), "packages", message="add at least one problem first")
    try:
        job_id, queued, reason = runtime().contest_build_service.queue(
            contest_id=contest_id,
            contest_slug=str(ctx["contest"]["slug"]),
            actor_user_id=int(ctx["user"]["id"]),
            outputs=requested_outputs,
            language=current_language,
            insert_blank_pages=insert_blank_pages,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conflict = reason in {
        "already_running",
        "busy",
    } or reason.startswith("not_ready:")
    if queued:
        message = "contest build queued"
    elif not conflict:
        message = f"contest build queue rejected ({reason})"
    else:
        detail: object = {
            "reason": reason,
            "active_job_id": job_id if reason == "already_running" else "",
        }
        raise HTTPException(status_code=409, detail=detail)
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "packages",
        query=_contest_packages_statement_query(language=current_language, job_id=job_id),
        fragment="job-report",
        message=message,
    )


def contest_packages_job_status(contest: str, user: Annotated[str, Depends(require_session_user)], job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    job = runtime().contest_service.load_job(contest_id, job_id.strip())
    if job is None:
        return JSONResponse({"ok": False, "running": False, "job_id": job_id.strip(), "status": "missing"}, status_code=404)
    status = job["status"]
    summary = job.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    return JSONResponse(
        {
            "ok": True,
            "running": status == "running",
            "job_id": job.get("id"),
            "job_type": job.get("job_type"),
            "status": status,
            "error": str(summary.get("error") or ""),
            "summary": summary,
            "created_at": job.get("created_at"),
            "finished_at": job.get("finished_at"),
        }
    )


def contest_packages_artifact_download(contest: str, user: Annotated[str, Depends(require_session_user)], artifact_id: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not ctx["access"]["can_download_packages"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["package_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    artifact = runtime().contest_service.artifact_download(contest_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="contest artifact not found")
    file_path, filename = artifact
    return FileResponse(file_path, filename=filename)


def contest_statement_source_file(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: str = "",
    path: str = "",
):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    key = _contest_statement_source_key(contest_id=contest_id, language=current_language, path=path, default_filename="statements.tex")
    display_path = _contest_statement_display_path(key, current_language)
    source_path = runtime().contest_service.statement_file_path(str(ctx["contest"]["slug"]), key)
    if source_path.exists() and source_path.is_file() and (not source_path.is_symlink()):
        return FileResponse(source_path, filename=Path(display_path).name)
    default_text = _contest_default_statement_source_text(contest_id, str(ctx["contest"]["slug"]), current_language, display_path)
    if default_text and runtime().contest_service.statement_source_is_text(key):
        return PlainTextResponse(
            default_text,
            media_type="text/plain; charset=utf-8",
            headers={"content-disposition": f'inline; filename="{Path(display_path).name}"'},
        )
    raise HTTPException(status_code=404, detail="contest statement source not found")


def contest_statement_source_save(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()] = "",
    path: Annotated[str, Form()] = "statements.tex",
    content: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    message = "contest statement source saved"
    display_path = str(path or "").strip() or "statements.tex"
    try:
        key = _contest_statement_source_key(contest_id=contest_id, language=current_language, path=display_path, default_filename="statements.tex")
        display_path = _contest_statement_display_path(key, current_language)
        if not runtime().contest_service.statement_source_is_text(key):
            raise ValueError("contest statement source is not a text file")
        text = enforce_textarea_max_bytes(
            content,
            label=f"contest statement source {display_path}",
            max_bytes=runtime().config_values.integer("TEXTAREA_MAX_BYTES"),
        )
        runtime().contest_service.write_statement_source_file(
            contest_id=contest_id,
            contest_slug=str(ctx["contest"]["slug"]),
            actor_user_id=int(ctx["user"]["id"]),
            key=key,
            package_bytes=text.encode("utf-8"),
        )
    except (ValueError, OSError) as exc:
        message = str(exc)
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "packages",
        query=_contest_packages_statement_query(language=current_language, source_path=display_path),
        message=message,
    )


async def contest_statement_source_upload(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    upload: Annotated[UploadFile, File()],
    language: Annotated[str, Form()] = "",
    path: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    message = "contest statement source uploaded"
    display_path = ""
    try:
        key = _contest_statement_source_key(
            contest_id=contest_id,
            language=current_language,
            path=path,
            default_filename="",
            upload_filename=upload.filename or "",
        )
        display_path = _contest_statement_display_path(key, current_language)
        payload = await read_upload_bytes_limited(
            upload,
            label="contest statement source",
            max_bytes=runtime().config_values.integer("UPLOAD_MAX_BYTES"),
        )
        runtime().contest_service.write_statement_source_file(
            contest_id=contest_id,
            contest_slug=str(ctx["contest"]["slug"]),
            actor_user_id=int(ctx["user"]["id"]),
            key=key,
            package_bytes=payload,
        )
    except (ValueError, OSError) as exc:
        message = str(exc)
    finally:
        await upload.close()
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "packages",
        query=_contest_packages_statement_query(language=current_language, source_path=display_path or "statements.tex"),
        message=message,
    )


def contest_statement_source_delete(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()] = "",
    path: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    message = "contest statement source deleted"
    try:
        key = _contest_statement_source_key(contest_id=contest_id, language=current_language, path=path, default_filename="")
        display_path = _contest_statement_display_path(key, current_language)
        runtime().contest_service.delete_statement_source_file(
            contest_id=contest_id,
            contest_slug=str(ctx["contest"]["slug"]),
            key=key,
        )
    except (ValueError, OSError) as exc:
        message = str(exc)
        display_path = "statements.tex"
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "packages",
        query=_contest_packages_statement_query(language=current_language, source_path=display_path),
        message=message,
    )


def contest_packages_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)], job_id: str = "", language: str = "", source_path: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    requested_job_id = str(job_id).strip()
    current_language = _contest_statement_language(contest_id, language)
    selected_source_error = ""
    try:
        selected_source_key = _contest_statement_source_key(
            contest_id=contest_id,
            language=current_language,
            path=source_path,
            default_filename="statements.tex",
        )
    except ValueError as exc:
        selected_source_error = str(exc)
        selected_source_key = _contest_statement_source_key(
            contest_id=contest_id,
            language=current_language,
            path="statements.tex",
            default_filename="statements.tex",
        )
    selected_display_path = _contest_statement_display_path(selected_source_key, current_language)
    selected_is_text = runtime().contest_service.statement_source_is_text(selected_source_key)
    selected_source_exists = False
    selected_source_text = ""
    if selected_is_text:
        default_text = _contest_default_statement_source_text(contest_id, str(ctx["contest"]["slug"]), current_language, selected_display_path)
        try:
            selected_path = runtime().contest_service.statement_file_path(str(ctx["contest"]["slug"]), selected_source_key)
            if selected_path.exists() and selected_path.is_file() and (not selected_path.is_symlink()):
                selected_source_text = selected_path.read_text(encoding="utf-8", errors="replace")
                selected_source_exists = True
            else:
                selected_source_text = default_text
        except OSError:
            selected_source_text = default_text
    artifact_rows = runtime().contest_service.list_artifacts(contest_id, limit=50)
    job_rows = runtime().contest_service.list_jobs(contest_id, limit=20)
    selected_job = runtime().contest_service.load_job(contest_id, requested_job_id)
    if selected_job is None and job_rows:
        selected_job = runtime().contest_service.load_job(contest_id, str(job_rows[0]["id"]))
    display_artifacts: list[dict[str, object]] = []
    for row in artifact_rows:
        item = dict(row)
        safe_id = str(item["id"] or "")
        item["download_href"] = (
            f"/contests/{ctx['contest']['slug']}/packages/artifacts/{safe_id}"
            if bool(item["downloadable"])
            else ""
        )
        display_artifacts.append(item)
    return template_response(
        request,
        "contest_packages.html",
        {
            "ctx": ctx,
            "artifact_rows": display_artifacts,
            "job_rows": job_rows,
            "selected_job": selected_job,
            "problem_count": runtime().contest_service.problem_count(contest_id),
            "requested_job_id": requested_job_id,
            "contest_statement_language": current_language,
            "contest_statement_language_options": _contest_statement_language_options(contest_id, current_language),
            "contest_statement_source_rows": _contest_statement_source_rows(contest_id, str(ctx["contest"]["slug"]), current_language),
            "contest_statement_selected_path": selected_display_path,
            "contest_statement_selected_key": selected_source_key,
            "contest_statement_selected_is_text": selected_is_text,
            "contest_statement_selected_exists": selected_source_exists,
            "contest_statement_selected_text": selected_source_text,
            "contest_statement_source_error": selected_source_error,
        },
    )
