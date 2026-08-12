from __future__ import annotations

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app.impl.auth.shared import (
    enforce_same_origin_state_change,
    redirect_response,
    template_response,
)
from app.impl.run_export.import_source import (
    build_import_slug_hint,
    import_package_as_new_problem,
    import_package_warnings,
)
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.context_operation import user_participating_problems
from app.impl.root.shared import _active_root_user, _count_label
from app.service.importing.upload import spool_fileobj
from app.service.importing.archive import ArchiveView, problem_import_policy



def problems_root_page(request: Request, user: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    raw_entries = user_participating_problems(int(gctx['user']['id']), limit=runtime().config_values.API_PROBLEMS_LIST_LIMIT)
    entries: list[dict[str, object]] = []
    owner_prefix_chars = 0
    for row in raw_entries:
        item = dict(row)
        slug = str(item["slug"])
        owner, leaf = slug.split("/", 1)
        item["slug_owner"] = owner
        item["slug_leaf"] = leaf
        entries.append(item)
        owner_prefix_chars = max(owner_prefix_chars, len(owner) + 1)
    return template_response(
        request,
        'root_problems.html',
        {
            'user': gctx['user'],
            'default_problem': gctx['default_problem'],
            'entries': entries,
            'entries_limit': runtime().config_values.API_PROBLEMS_LIST_LIMIT,
            'active_main': 'problems',
            'owner_prefix_chars': owner_prefix_chars,
        },
    )


def problems_root_import_slug_hint(request: Request, user: str = "", filename: str = "", requested_slug: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    payload = build_import_slug_hint(str(gctx["user"]["username"]), filename, requested_slug)
    return JSONResponse(payload)


def problems_root_import(request: Request, user: str = "", package_upload: UploadFile | None = File(None), problem_slug: str = Form("")):
    enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    package_name = ""
    try:
        if package_upload is None:
            raise ValueError("package file is required")
        package_name = str(package_upload.filename or "").strip()
        if not package_name:
            raise ValueError("package filename is required")
        snapshot = runtime().config_values.snapshot()
        upload_root = runtime().storage_layout.archive_upload_root
        with spool_fileobj(
            package_upload.file,
            root=upload_root,
            max_bytes=int(snapshot["UPLOAD_MAX_BYTES"]),
            label="package file",
        ) as package_path:
            policy = problem_import_policy(
                int(snapshot["PROBLEM_ZIP_MAX_EXPANDED_BYTES"]),
                int(snapshot["TEXTAREA_MAX_BYTES"]),
                int(snapshot["STATEMENT_SAMPLE_MAX_BYTES"]),
            )
            with ArchiveView(package_path, policy.archive) as package:
                imported = import_package_as_new_problem(
                    actor_user=str(gctx["user"]["username"]),
                    package_name=package_name,
                    package=package,
                    policy=policy,
                    requested_slug=str(problem_slug or "").strip(),
                )
        target_problem_obj = imported.get("target_problem")
        target_problem = target_problem_obj.strip() if isinstance(target_problem_obj, str) else ""
        total_tests_obj = imported.get("total_tests")
        total_tests = int(total_tests_obj) if isinstance(total_tests_obj, int) else 0
        package_format_obj = imported.get("package_format")
        package_format = package_format_obj.strip() if isinstance(package_format_obj, str) else "package"
        msg = f"{package_format} package imported as {target_problem} ({_count_label(total_tests, 'test')})"
        warnings = import_package_warnings(imported)
        if warnings:
            msg = f"{msg}; warning: {'; '.join(warnings)}"
        return redirect_response(f"/problems/{target_problem}/statement", status_code=303, message=msg)
    except Exception as exc:
        msg = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return redirect_response("/problems", status_code=303, message=msg)
