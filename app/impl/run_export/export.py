from __future__ import annotations

from app.impl.run_export.context import (
    Path,
    Form,
    Request,
    audit,
    git_commit_count,
    latest_workspace_committed_build,
    redirect_response,
    require_write_access,
    start_export_job,
    template_response,
    config,
    json,
    page_ctx,
    zipfile,
)
from app.impl.run_export.query import (
    _build_runtime_progress,
    _count_label,
    _summary_object,
    _verification_href_for_build,
)
def _export_recent_events(
    problem_id: int,
    actor_user_id: int,
    *,
    problem_slug: str,
    username: str,
    limit: int = 20,
) -> list[dict[str, object]]:
    cap = max(1, min(100, int(limit)))
    rows = config.db.fetch_all(
        """
        SELECT created_at,details_json
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='export.create'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(actor_user_id), cap],
    )
    result: list[dict[str, object]] = []
    resolved_commit_keys: set[tuple[str, str]] = set()
    for row in rows:
        item = dict(row)
        details = _summary_object(item.get("details_json"))
        status = str(details.get("status") or "").strip().lower() or "unknown"
        export_type = str(details.get("export_type") or "icpc").strip().lower() or "icpc"
        source_commit = str(details.get("source_commit") or "").strip()
        commit_key = (export_type, source_commit) if source_commit else ("", "")
        if status == "running" and source_commit and commit_key in resolved_commit_keys:
            continue
        build_id = str(details.get("build_id") or "").strip()
        if (not build_id) and status == "running" and source_commit:
            build_row = config.db.fetch_one(
                """
                SELECT id
                FROM builds
                WHERE problem_id=? AND source_commit=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [int(problem_id), source_commit],
            )
            if build_row is not None:
                build_id = str(build_row["id"] or "").strip()
        filename = str(details.get("filename") or "").strip()
        error_text = str(details.get("error") or "").strip()
        detail = filename if filename else (error_text if error_text else "-")
        runtime_progress = _build_runtime_progress(
            problem_id=int(problem_id),
            problem_slug=str(problem_slug or "").strip(),
            username=str(username or "").strip(),
            build_id=build_id,
            event_status=status,
        )
        runtime_detail = str(runtime_progress.get("detail") or "").strip()
        log_href = str(runtime_progress.get("log_href") or "").strip()
        if runtime_detail:
            detail = runtime_detail
        verification_href = _verification_href_for_build(
            problem_id=int(problem_id),
            problem_slug=str(problem_slug or "").strip(),
            username=str(username or "").strip(),
            build_id=build_id,
        )
        result.append(
            {
                "created_at": item.get("created_at"),
                "status": status,
                "status_upper": status.upper(),
                "source_commit": source_commit,
                "source_commit_short": source_commit[:8] if source_commit else "-",
                "build_id": build_id or "-",
                "detail": detail,
                "running": status == "running",
                "verification_href": verification_href,
                "log_href": log_href,
            }
        )
        if status in {"ok", "failed"} and source_commit:
            resolved_commit_keys.add(commit_key)
    return result

def _build_validation_status(build_row: dict[str, object] | None) -> str:
    if not isinstance(build_row, dict):
        return "validation unknown"
    status = str(build_row.get("status") or "").strip().lower()
    summary_raw = str(build_row.get("summary_json") or "").strip()
    summary: dict[str, object] = {}
    if summary_raw:
        try:
            parsed = json.loads(summary_raw)
            if isinstance(parsed, dict):
                summary = parsed
        except Exception:
            summary = {}
    steps = summary.get("steps")
    if isinstance(steps, list):
        for raw in steps:
            if not isinstance(raw, dict):
                continue
            step_name = str(raw.get("step") or "").strip().lower()
            if step_name != "validate":
                continue
            step_status = str(raw.get("status") or "").strip().lower()
            if step_status == "ok":
                return "validation passed"
            if step_status in {"error", "failed"}:
                return "validation failed"
            break
    failed_step = str(summary.get("failed_step") or "").strip().lower()
    if failed_step == "validate":
        return "validation failed"
    if status == "ok":
        return "validation passed"
    return "validation unknown"

def _export_archive_summary(problem: str, build_id: str, filename: str) -> dict[str, object]:
    result: dict[str, object] = {
        "available": False,
        "has_pdf": False,
        "solutions_total": None,
        "solutions_correct": None,
        "tests_total": None,
    }
    safe_build = str(build_id or "").strip()
    safe_filename = Path(str(filename or "").strip()).name
    if not safe_build or not safe_filename:
        return result
    archive_path = _resolve_export_archive_path(problem, safe_build, safe_filename)
    if archive_path is None:
        return result
    if not archive_path.exists() or not archive_path.is_file() or archive_path.is_symlink():
        return result
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = [str(name or "") for name in zf.namelist() if str(name or "") and (not str(name or "").endswith("/"))]
    except Exception:
        return result
    if not names:
        return result
    package_root = ""
    for name in names:
        if name.endswith("/problem.yaml"):
            package_root = name.split("/", 1)[0]
            break
    if not package_root:
        package_root = names[0].split("/", 1)[0]
    if not package_root:
        return result
    prefix = f"{package_root}/"
    has_pdf = f"{package_root}/statement/problem.en.pdf" in names
    solutions_total = 0
    solutions_correct = 0
    tests_total = 0
    for name in names:
        if not name.startswith(prefix):
            continue
        if name.startswith(f"{package_root}/submissions/"):
            solutions_total += 1
            if name.startswith(f"{package_root}/submissions/accepted/"):
                solutions_correct += 1
        if name.startswith(f"{package_root}/data/secret/") and name.endswith(".in"):
            tests_total += 1
    result["available"] = True
    result["has_pdf"] = bool(has_pdf)
    result["solutions_total"] = int(solutions_total)
    result["solutions_correct"] = int(solutions_correct)
    result["tests_total"] = int(tests_total)
    return result

def _resolve_export_archive_path(problem: str, build_id: str, filename: str) -> Path | None:
    safe_build = str(build_id or "").strip()
    safe_name = Path(str(filename or "").strip()).name
    if (not safe_build) or (not safe_name):
        return None
    row = config.db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [safe_build])
    if row is None:
        return None
    build_ref = str(row["build_ref"] or "").strip().lower()
    if not build_ref:
        return None
    try:
        root = config.fs_manager.build_paths(build_ref).root.resolve()
    except Exception:
        return None
    export_dir = (root / "export").resolve()
    if root != export_dir and root not in export_dir.parents:
        return None
    if (not export_dir.exists()) or (not export_dir.is_dir()) or export_dir.is_symlink():
        return None
    candidate = (export_dir / safe_name).resolve()
    if export_dir != candidate and export_dir not in candidate.parents:
        return None
    if (not candidate.exists()) or (not candidate.is_file()) or candidate.is_symlink():
        return None
    return candidate

def export_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx['workspace']['id']
    problem_id = int(ctx['problem']['id'])
    actor_user_id = int(ctx["user"]["id"])
    head_commit = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace = Path(ctx['workspace']['path'])
    generate_revision: int | None = git_commit_count(workspace, head_commit) if head_commit else None
    generate_revision_display = f'v{generate_revision}' if isinstance(generate_revision, int) and generate_revision >= 0 else 'missing'
    active_build = latest_workspace_committed_build(problem_id, int(workspace_id), head_commit, ok_only=True)
    build_status = 'ready' if active_build is not None else 'missing'
    if not head_commit:
        build_note = 'no committed revision yet; commit changes before generating package'
    elif active_build is None:
        build_note = 'no committed tests snapshot for this revision; Generate will build from committed revision'
    else:
        build_note = 'committed revision tests are ready for export'
    exports_rows = config.db.fetch_all('\n        SELECT id,build_id,export_type,filename,sha256,size_bytes,source_commit,created_at\n        FROM exports\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT 40\n        ', [ctx['problem']['id'], workspace_id])
    revision_cache: dict[str, int | None] = {}
    build_meta_cache: dict[str, dict[str, object] | None] = {}
    archive_summary_cache: dict[tuple[str, str], dict[str, object]] = {}
    exports: list[dict[str, object]] = []
    for row in exports_rows:
        item = dict(row)
        source_commit = str(item.get('source_commit') or '').strip()
        revision = None
        if source_commit:
            if source_commit in revision_cache:
                revision = revision_cache[source_commit]
            else:
                revision = git_commit_count(workspace, source_commit)
                revision_cache[source_commit] = revision
        item['revision'] = revision
        item['revision_display'] = f'v{revision}' if isinstance(revision, int) and revision >= 0 else 'v?'
        stored_filename = Path(str(item.get("filename") or "").strip()).name
        fallback_stem = Path(str(ctx["problem"]["slug"] or "")).name or "problem"
        item['display_filename'] = stored_filename or f"{fallback_stem}-{item['revision_display']}.zip"
        build_id = str(item.get("build_id") or "").strip()
        build_meta = build_meta_cache.get(build_id)
        if build_id and (build_meta is None) and (build_id not in build_meta_cache):
            row_meta = config.db.fetch_one(
                "SELECT id,status,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
                [build_id, problem_id, workspace_id],
            )
            build_meta = dict(row_meta) if row_meta is not None else None
            build_meta_cache[build_id] = build_meta
        validation_status = _build_validation_status(build_meta)
        summary_bits: list[str] = [validation_status]
        summary_key = (build_id, str(item.get("filename") or "").strip())
        archive_summary = archive_summary_cache.get(summary_key)
        if archive_summary is None:
            archive_summary = _export_archive_summary(problem, summary_key[0], summary_key[1])
            archive_summary_cache[summary_key] = archive_summary
        if bool(archive_summary.get("available")):
            summary_bits.insert(0, "has pdf" if bool(archive_summary.get("has_pdf")) else "no pdf")
            solutions_total = archive_summary.get("solutions_total")
            solutions_correct = archive_summary.get("solutions_correct")
            tests_total = archive_summary.get("tests_total")
            if isinstance(solutions_total, int) and isinstance(solutions_correct, int):
                summary_bits.append(f"{_count_label(solutions_total, 'solution')} ({solutions_correct} correct)")
            if isinstance(tests_total, int):
                summary_bits.append(_count_label(tests_total, "test"))
        item["summary_display"] = f"{item['revision_display']} ({', '.join(summary_bits)})" if summary_bits else item["revision_display"]
        exports.append(item)
    export_events = _export_recent_events(
        problem_id,
        actor_user_id,
        problem_slug=str(ctx["problem"]["slug"]),
        username=str(ctx["user"]["username"]),
        limit=20,
    )
    return template_response(
        request,
        'export.html',
        {
            'ctx': ctx,
            'active_build': active_build,
            'build_status': build_status,
            'build_note': build_note,
            'generate_revision_display': generate_revision_display,
            'exports': exports,
            'export_events': export_events,
        },
    )

def export_create(problem: str, user: str, build_id: str=Form(''), export_type: str=Form('icpc')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    require_write_access(ctx)
    resolved_build_id = str(build_id or '').strip()
    requested_export_type = str(export_type or '').strip().lower()
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    head_commit = str(ctx['workspace'].get('head_commit') or '').strip()
    if not requested_export_type:
        requested_export_type = 'icpc'
    initial_details: dict[str, object] = {'status': 'running', 'build_id': resolved_build_id, 'export_type': requested_export_type, 'source_commit': head_commit, 'filename': '', 'error': ''}
    try:
        if requested_export_type != 'icpc':
            raise ValueError('unsupported package type (ICPC only)')
        if not head_commit:
            raise ValueError('no committed revision; commit changes first')
        started = start_export_job(problem, user, actor_user_id=int(ctx['user']['id']), problem_id=problem_id, workspace_id=workspace_id, head_commit=head_commit, requested_build_id=resolved_build_id, requested_export_type=requested_export_type, initial_details=initial_details)
        msg = 'package generation queued' if started else 'package generation already running for this revision'
    except ValueError as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
        msg = str(exc)
    except Exception as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/export', status_code=303, message=msg)


