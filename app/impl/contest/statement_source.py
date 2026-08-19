from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from app.impl.auth.session import require_session_user
from app.impl.contest.shared import _contest_ctx, _contest_redirect
from app.impl.runtime.dependency import runtime
from app.main_util import enforce_textarea_max_bytes, read_upload_bytes_limited
from app.service.contest.service import CONTEST_STATEMENT_SHARED_SCOPE
from app.service.statement.constant import DEFAULT_OLYMP_STY
from app.service.statement.context import normalize_statement_language


def _contest_statement_sources_query(
    *,
    language: str,
    source_path: str = "",
) -> str:
    params: dict[str, str] = {}
    if language == CONTEST_STATEMENT_SHARED_SCOPE:
        params["scope"] = "all"
    else:
        safe_language = normalize_statement_language(language)
        if safe_language:
            params["language"] = safe_language
    if source_path:
        params["source_path"] = source_path
    return urlencode(params)


def _contest_statement_language(contest_id: int, language: str) -> str:
    if language == CONTEST_STATEMENT_SHARED_SCOPE:
        return CONTEST_STATEMENT_SHARED_SCOPE
    return runtime().contest_statement_service.resolve_language(contest_id, language)


def _contest_statement_source_key(
    *,
    contest_id: int,
    language: str,
    path: str,
    default_filename: str = "statements.tex",
    upload_filename: str = "",
) -> str:
    return runtime().contest_service.normalize_statement_source_key(
        language=_contest_statement_language(contest_id, language),
        path=path,
        upload_filename=upload_filename,
        default_filename=default_filename,
    )


def _contest_statement_display_path(key: str, language: str) -> str:
    safe_language = (
        CONTEST_STATEMENT_SHARED_SCOPE
        if language == CONTEST_STATEMENT_SHARED_SCOPE
        else normalize_statement_language(language)
    )
    if not safe_language:
        return key
    prefix = f"statements/{safe_language}/"
    if key.startswith(prefix):
        return key[len(prefix) :]
    return key


def contest_statement_language_options(
    contest_id: int,
    requested_language: str,
    additional_languages: tuple[str, ...] = (),
) -> list[str]:
    available: set[str] = set()
    for raw_language in additional_languages:
        language = normalize_statement_language(raw_language)
        if language:
            available.add(language)
    for attachment in runtime().contest_service.statement_attachment_rows(contest_id):
        parts = Path(str(attachment.get("rel_path") or "")).parts
        if len(parts) >= 3 and parts[0] == "statements":
            if parts[1] == CONTEST_STATEMENT_SHARED_SCOPE:
                continue
            language = normalize_statement_language(parts[1])
            if language:
                available.add(language)
    for problem in runtime().contest_service.contest_problems(contest_id):
        for raw_language in runtime().workspace_service.committed_statement_languages(
            problem["problem_slug"]
        ):
            language = normalize_statement_language(raw_language)
            if language:
                available.add(language)
    explicit = normalize_statement_language(requested_language)
    if explicit:
        available.add(explicit)
    ordered = sorted(available)
    if explicit:
        return [explicit, *[language for language in ordered if language != explicit]]
    if "english" in available:
        return ["english", *[language for language in ordered if language != "english"]]
    return ordered


def _contest_default_statement_source_text(
    contest_id: int,
    contest_slug: str,
    language: str,
    display_path: str,
) -> str:
    if language == CONTEST_STATEMENT_SHARED_SCOPE:
        return ""
    if display_path == "olymp.sty":
        return DEFAULT_OLYMP_STY
    if display_path != "statements.tex":
        return ""
    return runtime().contest_statement_service.default_statements_template()


def _contest_statement_source_rows(
    contest_id: int,
    contest_slug: str,
    language: str,
) -> list[dict[str, object]]:
    prefix = f"statements/{language}/"
    stored_rows: dict[str, dict[str, object]] = {}
    for row in runtime().contest_service.statement_attachment_rows(contest_id):
        key = str(row.get("rel_path") or "").strip()
        if not key.startswith(prefix):
            continue
        display_path = key[len(prefix) :]
        size_bytes: int | None = None
        exists = False
        try:
            source_path = runtime().contest_service.statement_file_path(
                contest_slug,
                key,
            )
            exists = (
                source_path.exists()
                and source_path.is_file()
                and not source_path.is_symlink()
            )
            if exists:
                size_bytes = source_path.stat().st_size
        except OSError:
            exists = False
        stored_rows[display_path] = {
            "display_path": display_path,
            "is_text": runtime().contest_service.statement_source_is_text(key),
            "size_bytes": size_bytes,
            "created_at": str(row.get("created_at") or ""),
            "source_display": (
                "Missing"
                if not exists
                else "Custom"
                if display_path in {"statements.tex", "olymp.sty"}
                else "Uploaded"
            ),
            "source_tone": "" if exists else "danger",
            "stored": True,
        }

    if language != CONTEST_STATEMENT_SHARED_SCOPE:
        for display_path in ("statements.tex", "olymp.sty"):
            if display_path in stored_rows:
                continue
            default_text = _contest_default_statement_source_text(
                contest_id,
                contest_slug,
                language,
                display_path,
            )
            stored_rows[display_path] = {
                "display_path": display_path,
                "is_text": True,
                "size_bytes": len(default_text.encode("utf-8")),
                "created_at": "",
                "source_display": "Default",
                "source_tone": "muted",
                "stored": False,
            }

    for display_path, source_row in stored_rows.items():
        source_row["download_href"] = (
            f"/contests/{contest_slug}/properties/statement/files?"
            f"{urlencode({'language': language, 'path': display_path})}"
        )
        source_row["edit_href"] = (
            f"/contests/{contest_slug}/properties?"
            f"{_contest_statement_sources_query(language=language, source_path=display_path)}"
        )
        source_row["delete_message"] = (
            f"Delete {display_path}?"
            + (
                " The default template will be used instead."
                if display_path in {"statements.tex", "olymp.sty"}
                else ""
            )
        )

    default_order = {"statements.tex": 0, "olymp.sty": 1}
    return sorted(
        stored_rows.values(),
        key=lambda item: (
            default_order.get(str(item["display_path"]), len(default_order)),
            str(item["display_path"]),
        ),
    )


def contest_statement_source_context(
    *,
    contest_id: int,
    contest_slug: str,
    language: str,
    source_path: str,
    scope: str = "",
    additional_languages: tuple[str, ...] = (),
) -> dict[str, object]:
    language_options = contest_statement_language_options(
        contest_id,
        language,
        additional_languages,
    )
    is_shared = str(scope).strip().lower() == "all"
    explicit_language = normalize_statement_language(language)
    default_language = (
        "english"
        if "english" in language_options
        else language_options[0]
        if language_options
        else CONTEST_STATEMENT_SHARED_SCOPE
    )
    current_language = (
        CONTEST_STATEMENT_SHARED_SCOPE
        if is_shared
        else explicit_language or default_language
    )
    source_error = ""
    selected_key = ""
    selected_path = ""
    selected_is_text = False
    selected_exists = False
    selected_text = ""
    if source_path:
        try:
            selected_key = _contest_statement_source_key(
                contest_id=contest_id,
                language=current_language,
                path=source_path,
            )
        except ValueError as exc:
            source_error = str(exc)
        if selected_key:
            selected_path = _contest_statement_display_path(
                selected_key,
                current_language,
            )
            selected_is_text = runtime().contest_service.statement_source_is_text(
                selected_key
            )
    if selected_is_text:
        default_text = _contest_default_statement_source_text(
            contest_id,
            contest_slug,
            current_language,
            selected_path,
        )
        try:
            file_path = runtime().contest_service.statement_file_path(
                contest_slug,
                selected_key,
            )
            if file_path.exists() and file_path.is_file() and not file_path.is_symlink():
                selected_text = file_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                selected_exists = True
            else:
                selected_text = default_text
        except OSError:
            selected_text = default_text
    return {
        "contest_statement_language": current_language,
        "contest_statement_language_options": language_options,
        "contest_statement_source_is_shared": (
            current_language == CONTEST_STATEMENT_SHARED_SCOPE
        ),
        "contest_statement_source_query": _contest_statement_sources_query(
            language=current_language,
        ),
        "contest_statement_upload_scopes": [
            {
                "value": CONTEST_STATEMENT_SHARED_SCOPE,
                "label": "Shared",
                "selected": current_language == CONTEST_STATEMENT_SHARED_SCOPE,
            },
            *[
                {
                    "value": option,
                    "label": option.title(),
                    "selected": current_language == option,
                }
                for option in language_options
            ],
        ],
        "contest_statement_source_rows": _contest_statement_source_rows(
            contest_id,
            contest_slug,
            current_language,
        ),
        "contest_statement_selected_path": selected_path,
        "contest_statement_selected_key": selected_key,
        "contest_statement_selected_is_text": selected_is_text,
        "contest_statement_selected_exists": selected_exists,
        "contest_statement_selected_text": selected_text,
        "contest_statement_source_error": source_error,
    }


def statement_review_languages(ctx: dict[str, object]) -> tuple[str, ...]:
    groups = ctx.get("statement_review_link_groups")
    if not isinstance(groups, list):
        return ()
    languages: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        links = group.get("links")
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            language = normalize_statement_language(str(link.get("language") or ""))
            if language and language not in languages:
                languages.append(language)
    return tuple(languages)


def contest_statement_source_file(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: str = "",
    path: str = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    key = _contest_statement_source_key(
        contest_id=contest_id,
        language=current_language,
        path=path,
    )
    display_path = _contest_statement_display_path(key, current_language)
    file_path = runtime().contest_service.statement_file_path(
        str(ctx["contest"]["slug"]),
        key,
    )
    if file_path.exists() and file_path.is_file() and not file_path.is_symlink():
        return FileResponse(file_path, filename=Path(display_path).name)
    default_text = _contest_default_statement_source_text(
        contest_id,
        str(ctx["contest"]["slug"]),
        current_language,
        display_path,
    )
    if default_text and runtime().contest_service.statement_source_is_text(key):
        return PlainTextResponse(
            default_text,
            media_type="text/plain; charset=utf-8",
            headers={
                "content-disposition": f'inline; filename="{Path(display_path).name}"'
            },
        )
    raise HTTPException(status_code=404, detail="contest statement source not found")


def contest_statement_source_save(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()] = "",
    path: Annotated[str, Form()] = "statements.tex",
    content: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["write_block_reason"],
        )
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    message = "contest statement source saved"
    display_path = path.strip() or "statements.tex"
    try:
        key = _contest_statement_source_key(
            contest_id=contest_id,
            language=current_language,
            path=display_path,
        )
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
        "properties",
        query=_contest_statement_sources_query(
            language=current_language,
        ),
        message=message,
    )


async def contest_statement_source_upload(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    upload: Annotated[UploadFile, File()],
    language: Annotated[str, Form()] = "",
    path: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["write_block_reason"],
        )
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    message = "contest statement source uploaded"
    try:
        key = _contest_statement_source_key(
            contest_id=contest_id,
            language=current_language,
            path=path,
            default_filename="",
            upload_filename=upload.filename or "",
        )
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
        "properties",
        query=_contest_statement_sources_query(
            language=current_language,
        ),
        message=message,
    )


def contest_statement_source_delete(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()] = "",
    path: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["write_block_reason"],
        )
    contest_id = int(ctx["contest"]["id"])
    current_language = _contest_statement_language(contest_id, language)
    message = "contest statement source deleted"
    try:
        key = _contest_statement_source_key(
            contest_id=contest_id,
            language=current_language,
            path=path,
            default_filename="",
        )
        runtime().contest_service.delete_statement_source_file(
            contest_id=contest_id,
            contest_slug=str(ctx["contest"]["slug"]),
            key=key,
        )
    except (ValueError, OSError) as exc:
        message = str(exc)
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "properties",
        query=_contest_statement_sources_query(
            language=current_language,
        ),
        message=message,
    )


def contest_statement_language_remove(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["write_block_reason"],
        )
    contest_id = int(ctx["contest"]["id"])
    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise HTTPException(status_code=400, detail="contest statement language is required")
    removed = runtime().contest_service.delete_statement_language_sources(
        contest_id=contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        language=safe_language,
    )
    message = (
        f"removed {safe_language} Contest statement sources"
        if removed
        else f"no {safe_language} Contest statement sources were saved"
    )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "properties",
        query="scope=all",
        message=message,
    )
