import shutil
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import File, Form, Request, UploadFile

from app.impl.auth.shared import (
    enforce_same_origin_state_change,
    redirect_response,
    template_response,
)
from app.impl.workspace.context import GlobalUserPageContext
from app.impl.root.contest_import import (
    _build_contest_import_problem_draft_rows,
    _build_problem_slug_review_rows,
    _contest_slug_review_state,
    _create_contest_import_draft,
    _delete_contest_import_draft,
    _load_contest_import_draft,
    _normalize_import_contest_idx,
    _rollback_imported_contest,
    _resolve_import_contest_slug,
)
from app.impl.run_export.import_source import (
    import_package_as_new_problem,
    import_package_warnings,
)
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.context_operation import (
    normalize_contest_slug_required,
    normalize_contest_title_required,
    user_contests_overview,
)
from app.impl.root.shared import _active_root_user, _count_label
from app.main_util import form_text
from app.service.importing.archive import (
    ArchivePolicy,
    ArchiveView,
    ProblemImportPolicy,
    contest_archive_policy,
    problem_import_policy,
)
from app.service.importing.contest import (
    ImportedContestStatementFile,
    PolygonContestImportService,
)
from app.service.importing.upload import spool_fileobj

_POLYGON_CONTEST_IMPORT_SERVICE = PolygonContestImportService()


def _contest_archive_policies() -> tuple[ArchivePolicy, ProblemImportPolicy, int]:
    max_problems = runtime().config_values.integer("CONTEST_MAX_PROBLEMS")
    problem_expanded = runtime().config_values.integer(
        "PROBLEM_ZIP_MAX_EXPANDED_BYTES"
    )
    problem_policy = problem_import_policy(
        problem_expanded,
        runtime().config_values.integer("TEXTAREA_MAX_BYTES"),
        runtime().config_values.integer("STATEMENT_SAMPLE_MAX_BYTES"),
    )
    contest_policy = contest_archive_policy(max_problems, problem_expanded)
    return contest_policy, problem_policy, max_problems


def _render_contest_import_review_page(
    request: Request,
    gctx: GlobalUserPageContext,
    draft: dict[str, object],
    *,
    draft_id: str,
    contest_slug_input: str,
    contest_title_input: str,
    problem_slug_overrides: dict[int, str],
    top_error: str = "",
) -> object:
    package_name_obj = draft.get("package_name")
    package_name = package_name_obj.strip() if isinstance(package_name_obj, str) else ""
    draft_rows_raw = draft.get("problem_rows")
    draft_rows = [dict(item) for item in draft_rows_raw] if isinstance(draft_rows_raw, list) else []
    user_obj = gctx.get("user")
    owner = ""
    if isinstance(user_obj, dict):
        owner_obj = user_obj.get("username")
        if isinstance(owner_obj, str):
            owner = owner_obj.strip()
    rows, rows_have_error = _build_problem_slug_review_rows(owner, draft_rows, problem_slug_overrides)
    slug_state = _contest_slug_review_state(contest_slug_input, package_name)
    slug_input_value = contest_slug_input.strip() if isinstance(contest_slug_input, str) else ""
    if not slug_input_value:
        suggested_slug = slug_state.get("suggested")
        if isinstance(suggested_slug, str):
            slug_input_value = suggested_slug.strip()
    title_input_value = contest_title_input.strip() if isinstance(contest_title_input, str) else ""
    if not title_input_value:
        parsed_title_obj = draft.get("parsed_title")
        if isinstance(parsed_title_obj, str):
            title_input_value = parsed_title_obj.strip()
    contest_slug_error = ""
    if not bool(slug_state.get("valid")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    elif bool(slug_state.get("exists")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    has_error = bool(top_error or rows_have_error or contest_slug_error)
    parsed_title_obj = draft.get("parsed_title")
    parsed_title = parsed_title_obj.strip() if isinstance(parsed_title_obj, str) else ""
    return template_response(
        request,
        "contest_import_review.html",
        {
            "user": gctx["user"],
            "default_problem": gctx["default_problem"],
            "active_main": "contests",
            "draft_id": draft_id,
            "package_name": package_name,
            "parsed_title": parsed_title,
            "contest_slug_value": slug_input_value,
            "contest_slug_state": slug_state,
            "contest_slug_error": contest_slug_error,
            "contest_title_value": title_input_value,
            "problem_rows": rows,
            "top_error": str(top_error or "").strip(),
            "has_error": has_error,
        },
    )


def contests_root_page(request: Request, user: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    entries_limit = runtime().config_values.integer("API_PROBLEMS_LIST_LIMIT")
    entries = user_contests_overview(
        int(gctx['user']['id']),
        limit=entries_limit,
    )
    return template_response(request, 'root_contests.html', {'user': gctx['user'], 'default_problem': gctx['default_problem'], 'entries': entries, 'entries_limit': entries_limit, 'active_main': 'contests'})

def contests_root_create(request: Request, user: str = "", contest_slug: str = Form(...), contest_title: str = Form(...)):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    msg = "contest created"
    try:
        slug = normalize_contest_slug_required(contest_slug)
        title = normalize_contest_title_required(contest_title)
        actor_user_id = int(gctx["user"]["id"])
        runtime().contest_service.create_contest_with_owner(
            slug=slug,
            title=title,
            owner_user_id=actor_user_id,
        )
        msg = f"contest {slug} created"
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
    return redirect_response("/contests", status_code=303, message=msg)


def contests_root_import(
    request: Request,
    user: str = "",
    package_upload: UploadFile | None = File(None),
    contest_slug: str = Form(""),
    contest_title: str = Form(""),
):
    enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    package_name = ""
    try:
        if package_upload is None:
            raise ValueError("package file is required")
        package_name = str(package_upload.filename or "").strip()
        if not package_name:
            raise ValueError("package filename is required")
        contest_policy, problem_policy, max_problems = _contest_archive_policies()
        with spool_fileobj(
            package_upload.file,
            root=runtime().storage_layout.archive_upload_root,
            max_bytes=runtime().config_values.integer("UPLOAD_MAX_BYTES"),
            label="package file",
        ) as package_path:
            with ArchiveView(package_path, contest_policy) as package:
                parsed = _POLYGON_CONTEST_IMPORT_SERVICE.parse_package(
                    package_name,
                    package,
                    problem_policy=problem_policy.archive,
                    max_problems=max_problems,
                )
            rows = parsed.get("problems")
            if not isinstance(rows, list) or not rows:
                raise ValueError("contest package has no problems")
            if len(rows) > max_problems:
                raise ValueError(
                    f"contest package has more than the configured maximum of {max_problems} problems"
                )
            draft_rows = _build_contest_import_problem_draft_rows(actor_username, [dict(item) for item in rows if isinstance(item, dict)])
            if not draft_rows:
                raise ValueError("contest package has no importable problem rows")
            parsed_title_obj = parsed.get("title")
            parsed_title = parsed_title_obj.strip() if isinstance(parsed_title_obj, str) else ""
            draft_id = _create_contest_import_draft(
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                package_name=package_name,
                package_path=package_path,
                contest_slug_input=form_text(contest_slug).strip(),
                contest_title_input=form_text(contest_title).strip(),
                parsed_title=parsed_title,
                problem_rows=draft_rows,
            )
        message = f"contest package parsed ({_count_label(len(draft_rows), 'problem')}); review slugs before import"
        return redirect_response(
            f"/contests/import/review?draft_id={quote_plus(draft_id)}",
            status_code=303,
            message=message,
        )
    except Exception as exc:
        message = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return redirect_response("/contests", status_code=303, message=message)


def contests_root_import_review(request: Request, user: str = "", draft_id: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    safe_draft_id = str(draft_id or "").strip()
    try:
        draft, _payload_path = _load_contest_import_draft(actor_user_id, actor_username, safe_draft_id)
    except Exception as exc:
        return redirect_response("/contests", status_code=303, message=str(exc))
    return _render_contest_import_review_page(
        request,
        gctx,
        draft,
        draft_id=safe_draft_id,
        contest_slug_input=(
            str(draft["contest_slug_input"]).strip()
            if isinstance(draft.get("contest_slug_input"), str)
            else ""
        ),
        contest_title_input=(
            str(draft["contest_title_input"]).strip()
            if isinstance(draft.get("contest_title_input"), str)
            else ""
        ),
        problem_slug_overrides={},
    )


async def contests_root_import_confirm(request: Request, user: str = ""):
    enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    created_contest_slug = ""
    imported_problem_slugs: list[str] = []
    contest_archive: ArchiveView | None = None
    statement_staging: Path | None = None
    form = await request.form()
    draft_id_obj = form.get("draft_id")
    draft_id = draft_id_obj.strip() if isinstance(draft_id_obj, str) else ""
    contest_slug_obj = form.get("contest_slug")
    contest_slug_input = contest_slug_obj.strip() if isinstance(contest_slug_obj, str) else ""
    contest_title_obj = form.get("contest_title")
    contest_title_input = contest_title_obj.strip() if isinstance(contest_title_obj, str) else ""
    try:
        draft, payload_path = _load_contest_import_draft(actor_user_id, actor_username, draft_id)
    except Exception as exc:
        return redirect_response("/contests", status_code=303, message=str(exc))

    draft_rows_raw = draft.get("problem_rows")
    draft_rows = [dict(item) for item in draft_rows_raw] if isinstance(draft_rows_raw, list) else []
    problem_slug_overrides: dict[int, str] = {}
    for row in draft_rows:
        seq_obj = row.get("seq")
        seq = int(seq_obj) if isinstance(seq_obj, int) else 0
        if seq <= 0:
            continue
        key = f"problem_slug_{seq}"
        slug_override_obj = form.get(key)
        problem_slug_overrides[seq] = slug_override_obj.strip() if isinstance(slug_override_obj, str) else ""

    review_rows, rows_have_error = _build_problem_slug_review_rows(actor_username, draft_rows, problem_slug_overrides)
    draft_package_name_obj = draft.get("package_name")
    draft_package_name = draft_package_name_obj.strip() if isinstance(draft_package_name_obj, str) else ""
    slug_state = _contest_slug_review_state(contest_slug_input, draft_package_name)
    contest_slug_error = ""
    if not bool(slug_state.get("valid")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    elif bool(slug_state.get("exists")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    if rows_have_error or contest_slug_error:
        return _render_contest_import_review_page(
            request,
            gctx,
            draft,
            draft_id=draft_id,
            contest_slug_input=contest_slug_input,
            contest_title_input=contest_title_input,
            problem_slug_overrides=problem_slug_overrides,
        )

    package_name = draft_package_name
    try:
        contest_policy, problem_policy, max_problems = _contest_archive_policies()
        contest_archive = ArchiveView(payload_path, contest_policy)
        parsed = _POLYGON_CONTEST_IMPORT_SERVICE.parse_package(
            package_name,
            contest_archive,
            problem_policy=problem_policy.archive,
            max_problems=max_problems,
        )
        parsed_rows = [dict(item) for item in parsed["problems"]]
        statement_files: list[ImportedContestStatementFile] = list(
            parsed["statement_files"]
        )
        default_language_obj = parsed.get("default_language")
        default_language = default_language_obj.strip().lower() if isinstance(default_language_obj, str) else ""
        location_obj = parsed.get("location")
        inferred_location = location_obj.strip() if isinstance(location_obj, str) else ""
        date_obj = parsed.get("date")
        inferred_date = date_obj.strip() if isinstance(date_obj, str) else ""
        if len(parsed_rows) != len(review_rows):
            raise ValueError("contest package changed; please re-upload and review again")
        if len(parsed_rows) > max_problems:
            raise ValueError(
                f"contest package has more than the configured maximum of {max_problems} problems"
            )

        target_contest_slug = _resolve_import_contest_slug(contest_slug_input, package_name)
        parsed_title_obj = parsed.get("title")
        parsed_title = parsed_title_obj.strip() if isinstance(parsed_title_obj, str) else ""
        target_contest_title = normalize_contest_title_required(
            form_text(contest_title_input).strip() or parsed_title or target_contest_slug
        )

        contest_id = runtime().contest_service.create_contest_with_owner(
            slug=target_contest_slug,
            title=target_contest_title,
            owner_user_id=actor_user_id,
        )
        created_contest_slug = target_contest_slug

        import_warnings: list[str] = []
        used_indices: set[str] = set()
        source_folder_map: dict[int, str] = {}
        for idx, row in enumerate(parsed_rows, start=1):
            row_review = review_rows[idx - 1]
            sub_package_name_obj = row.get("package_name")
            sub_package_name = sub_package_name_obj.strip() if isinstance(sub_package_name_obj, str) else ""
            if not sub_package_name:
                sub_package_name = f"problem-{idx}.zip"
            requested_problem_slug_obj = row_review.get("slug_input")
            requested_problem_slug = requested_problem_slug_obj.strip().lower() if isinstance(requested_problem_slug_obj, str) else ""
            source_folder_obj = row.get("source_folder")
            source_folder = source_folder_obj.strip() if isinstance(source_folder_obj, str) else ""
            if not source_folder:
                raise RuntimeError(f"contest source folder missing for problem #{idx}")
            problem_archive = _POLYGON_CONTEST_IMPORT_SERVICE.problem_archive(
                contest_archive,
                source_folder,
                problem_policy.archive,
            )
            imported = import_package_as_new_problem(
                actor_user=actor_username,
                package_name=sub_package_name,
                package=problem_archive,
                policy=problem_policy,
                requested_slug=requested_problem_slug,
                normalize_test_data_newlines=True,
            )
            imported_problem_slug_obj = imported.get("target_problem")
            imported_problem_slug = imported_problem_slug_obj.strip() if isinstance(imported_problem_slug_obj, str) else ""
            if not imported_problem_slug:
                raise RuntimeError(f"failed to import problem package #{idx}")
            warnings = import_package_warnings(imported)
            for warning in warnings:
                import_warnings.append(f"{imported_problem_slug}: {warning}")
            problem_id = runtime().workspace_service.known_problem_id(imported_problem_slug)
            if problem_id is None:
                raise RuntimeError(f"imported problem missing: {imported_problem_slug}")
            contest_problem_idx = _normalize_import_contest_idx(row.get("index"), idx, used_indices)
            runtime().contest_service.add_problem(
                contest_id=contest_id,
                idx=contest_problem_idx,
                problem_id=problem_id,
                added_by_user_id=actor_user_id,
            )
            source_folder_map[int(problem_id)] = source_folder
            imported_problem_slugs.append(imported_problem_slug)

        statement_staging = payload_path.parent / f".{draft_id}-statement-{uuid.uuid4().hex}"
        staged_statement_files = _POLYGON_CONTEST_IMPORT_SERVICE.stage_statement_sources(
            contest_archive,
            statement_files,
            statement_staging,
        )
        runtime().contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=target_contest_slug,
            actor_user_id=actor_user_id,
            files=staged_statement_files,
        )
        runtime().contest_service.set_statement_default_language(contest_id, actor_user_id, default_language)
        if inferred_location:
            runtime().contest_service.upsert_property(contest_id, actor_user_id, "location", inferred_location)
        if inferred_date:
            runtime().contest_service.upsert_property(contest_id, actor_user_id, "date", inferred_date)
        runtime().contest_service.set_statement_problem_source_folders(contest_id, actor_user_id, source_folder_map)

        contest_archive.close()
        contest_archive = None
        _delete_contest_import_draft(draft_id)
        message = f"contest {target_contest_slug} imported ({_count_label(len(imported_problem_slugs), 'problem')})"
        if import_warnings:
            first_warning = str(import_warnings[0] or "").strip()
            extra = max(0, len(import_warnings) - 1)
            suffix = f" (+{extra} more)" if extra > 0 else ""
            message = f"{message}; warning: {first_warning}{suffix}"
        return redirect_response(
            f"/contests/{target_contest_slug}/overview",
            status_code=303,
            message=message,
        )
    except Exception as exc:
        message = str(exc)
    finally:
        if contest_archive is not None:
            contest_archive.close()
        if statement_staging is not None:
            shutil.rmtree(statement_staging, ignore_errors=True)
    if created_contest_slug or imported_problem_slugs:
        try:
            _rollback_imported_contest(created_contest_slug, imported_problem_slugs)
        finally:
            _delete_contest_import_draft(draft_id)
        return redirect_response("/contests", status_code=303, message=message)
    return _render_contest_import_review_page(
        request,
        gctx,
        draft,
        draft_id=draft_id,
        contest_slug_input=contest_slug_input,
        contest_title_input=contest_title_input,
        problem_slug_overrides=problem_slug_overrides,
        top_error=message,
    )
