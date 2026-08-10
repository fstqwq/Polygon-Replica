from __future__ import annotations

import secrets
from typing import Annotated, TypedDict
from urllib.parse import quote, quote_plus, urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.impl.auth.csrf import issue_password_form_csrf_token, verify_password_form_csrf_token
from app.impl.auth.password_envelope import password_envelope_store
from app.impl.auth.session import require_session_user
from app.impl.auth.shared import (
    lookup_user_auth,
    normalize_password_iters,
    normalize_password_salt_hex,
    normalize_username_required,
    redirect_response,
    set_user_password_verifier,
    template_response,
)
from app.impl.runtime.config import config
from app.impl.workspace.access import require_system_admin
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.context_operation import audit
from app.main_util import form_text
from app.service.verification.runtime import coerce_int


_C = config.constants


class JudgehostToolchainView(TypedDict):
    language_id: str
    language_label: str
    compiler: str
    compiler_summary: str
    runner: str
    runner_summary: str
    observed_at: str
    judgetask_id: int


class JudgehostView(TypedDict):
    hostname: str
    peer_addr: str
    enabled: bool
    online: bool
    state: str
    state_class: str
    age_label: str
    last_seen_at: str
    first_seen_at: str
    last_action: str
    last_action_label: str
    last_task_id: str
    last_run_id: str
    toolchains: list[JudgehostToolchainView]
    active_leases: int
    update_count: int
    judged_case_count: int
    last_judging_at: str
    last_judging: dict[str, object] | None
    last_judging_href: str
    recent_avg_per_case_sec: float | None


class ArtifactUsageView(TypedDict):
    total_size_label: str
    total_files_label: str
    artifacts_size_label: str
    cache_size_label: str
    verification_count_label: str
    removable_rows_label: str
    audit_rows_label: str


def _as_bool_form_value(raw: str) -> bool:
    return form_text(raw).strip().lower() in {"1", "true", "yes", "on"}


def _system_config_row_by_key(
    sections: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for section in sections:
        section_rows = section.get("rows")
        if not isinstance(section_rows, list):
            continue
        for row in section_rows:
            if not isinstance(row, dict):
                continue
            key = row.get("key")
            if isinstance(key, str) and key:
                rows[key] = row
    return rows


def _admin_user_context(user: str) -> tuple[dict[str, object], int]:
    ctx = global_user_ctx(user)
    require_system_admin(ctx)
    return ctx, int(ctx["user"]["id"])


def _config_sections() -> list[dict[str, object]]:
    config.system_config_service.refresh()
    sections = config.system_config_service.ui_sections()
    for section in sections:
        slug = section.get("slug")
        if isinstance(slug, str):
            section["href"] = f"/admin/config/{quote_plus(slug)}"
    return sections


def _admin_nav(active: str, *, config_href: str = "/admin/config") -> list[dict[str, object]]:
    entries = (
        ("overview", "Overview", "/admin"),
        ("judgehosts", "Judgehosts", "/admin/judgehosts"),
        ("users", "Users", "/admin/users"),
        ("mail", "Mail", "/admin/mail"),
        ("config", "Configuration", config_href),
    )
    return [
        {"key": key, "label": label, "href": href, "active": key == active}
        for key, label, href in entries
    ]


def _admin_page_context(
    user: str,
    active: str,
    *,
    config_sections: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], int]:
    ctx, actor_user_id = _admin_user_context(user)
    user_row = dict(ctx["user"])
    sections = config_sections if config_sections is not None else []
    config_href = "/admin/config"
    if sections:
        first_href = sections[0].get("href")
        if isinstance(first_href, str) and first_href:
            config_href = first_href
    return (
        {
            "user": user_row,
            "active_main": "admin",
            "admin_active": active,
            "admin_nav": _admin_nav(active, config_href=config_href),
        },
        actor_user_id,
    )


def _runtime_controls(
    sections: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    rows_by_key = _system_config_row_by_key(sections)
    controls: dict[str, dict[str, object]] = {}
    for key in ("JUDGEHOST_ENABLE", "JUDGEHOST_API_TOKEN", "JUDGEHOST_API_USERNAME"):
        row = rows_by_key.get(key, {})
        current_value = row.get("current_value")
        current_display = row.get("current_display")
        controls[key] = {
            "key": key,
            "description": row.get("description"),
            "choices": list(row["choices"]) if isinstance(row.get("choices"), list) else [],
            "current_value": current_value,
            "current_display": (
                current_display
                if isinstance(current_display, str)
                else current_value
                if isinstance(current_value, str)
                else None
            ),
            "changed": bool(row.get("changed")),
            "impact": row.get("impact"),
        }
    return controls


def _storage_size_label(num_bytes: int) -> str:
    size = max(0, num_bytes)
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable storage unit")


def _artifact_usage_view() -> ArtifactUsageView:
    usage = config.artifact_cleanup_service.usage_snapshot()
    return {
        "total_size_label": _storage_size_label(usage["total_bytes"]),
        "total_files_label": f"{usage['total_files']:,}",
        "artifacts_size_label": _storage_size_label(usage["artifacts_bytes"]),
        "cache_size_label": _storage_size_label(usage["cache_bytes"]),
        "verification_count_label": f"{usage['table_rows']['verifications']:,}",
        "removable_rows_label": f"{usage['removable_rows']:,}",
        "audit_rows_label": f"{usage['audit_rows']:,}",
    }


def _duration_label(age_sec: object) -> str:
    if not isinstance(age_sec, int) or age_sec < 0:
        return "unknown"
    if age_sec < 60:
        return f"{age_sec}s ago"
    minutes = age_sec // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m ago"
    days = hours // 24
    return f"{days}d {hours % 24}h ago"


def _version_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        if not line.startswith("command="):
            return line
    if lines:
        return lines[0].removeprefix("command=")
    return "not reported"


def _toolchain_views(raw_toolchains: object) -> list[JudgehostToolchainView]:
    if not isinstance(raw_toolchains, list):
        return []
    language_labels = {"c": "C", "cpp": "C++", "java": "Java", "py": "Python"}
    out: list[JudgehostToolchainView] = []
    for raw in raw_toolchains:
        if not isinstance(raw, dict):
            continue
        language_id = str(raw.get("language_id") or "")
        compiler = str(raw.get("compiler") or "")
        runner = str(raw.get("runner") or "")
        out.append(
            {
                "language_id": language_id,
                "language_label": language_labels.get(language_id, language_id or "Unknown"),
                "compiler": compiler,
                "compiler_summary": _version_summary(compiler),
                "runner": runner,
                "runner_summary": _version_summary(runner),
                "observed_at": str(raw.get("observed_at") or ""),
                "judgetask_id": int(raw.get("judgetask_id") or 0),
            }
        )
    return out


def _last_judging_href(last_judging: dict[str, object] | None) -> str:
    if last_judging is None:
        return ""
    problem_slug = str(last_judging.get("problem_slug") or "")
    verification_id = str(last_judging.get("verification_id") or "")
    if not problem_slug or not verification_id:
        return ""
    problem_path = quote(problem_slug, safe="/")
    query = urlencode({"verification_id": verification_id})
    return f"/problems/{problem_path}/run/details?{query}"


def _judgehost_status_view() -> dict[str, object]:
    raw_status = config.judgehost_task_service.status()
    raw_hosts = raw_status.get("hosts")
    hosts: list[JudgehostView] = []
    action_labels = {
        "register": "registered",
        "fetch": "waiting for work",
        "lease": "leased work",
        "update": "reported progress",
        "report": "reported result",
        "versions": "reported toolchains",
        "debug": "reported diagnostics",
        "internal-error": "reported internal error",
        "disabled": "polled while disabled",
        "set-enabled": "enabled by admin",
        "set-disabled": "disabled by admin",
    }
    if isinstance(raw_hosts, list):
        for raw in raw_hosts:
            if not isinstance(raw, dict):
                continue
            enabled = bool(raw.get("enabled"))
            online = bool(raw.get("online"))
            state = "disabled" if not enabled else "online" if online else "offline"
            last_action = str(raw.get("last_action") or "")
            raw_last_judging = raw.get("last_judging")
            last_judging = dict(raw_last_judging) if isinstance(raw_last_judging, dict) else None
            recent_avg_raw = raw.get("recent_avg_per_case_sec")
            recent_avg = float(recent_avg_raw) if isinstance(recent_avg_raw, (int, float)) else None
            hosts.append(
                {
                    "hostname": str(raw.get("hostname") or ""),
                    "peer_addr": str(raw.get("peer_addr") or ""),
                    "enabled": enabled,
                    "online": online,
                    "state": state,
                    "state_class": "muted" if state == "disabled" else "ok" if state == "online" else "danger",
                    "age_label": _duration_label(raw.get("age_sec")),
                    "last_seen_at": str(raw.get("last_seen_at") or ""),
                    "first_seen_at": str(raw.get("first_seen_at") or ""),
                    "last_action": last_action,
                    "last_action_label": action_labels.get(last_action, last_action.replace("-", " ") or "unknown"),
                    "last_task_id": str(raw.get("last_task_id") or ""),
                    "last_run_id": str(raw.get("last_run_id") or ""),
                    "toolchains": _toolchain_views(raw.get("toolchains")),
                    "active_leases": int(raw.get("active_leases") or 0),
                    "update_count": int(raw.get("update_count") or 0),
                    "judged_case_count": int(raw.get("judged_case_count") or 0),
                    "last_judging_at": str(raw.get("last_judging_at") or ""),
                    "last_judging": last_judging,
                    "last_judging_href": _last_judging_href(last_judging),
                    "recent_avg_per_case_sec": recent_avg,
                }
            )
    raw_queue = raw_status.get("queue")
    queue = dict(raw_queue) if isinstance(raw_queue, dict) else {}
    return {
        "enabled": bool(raw_status.get("enabled")),
        "auth_configured": bool(raw_status.get("auth_configured")),
        "hosts_total": int(raw_status.get("hosts_total") or 0),
        "hosts_online": int(raw_status.get("hosts_online") or 0),
        "hosts": hosts,
        "queue": {
            "queued": int(queue.get("queued") or 0),
            "leased": int(queue.get("leased") or 0),
            "completed": int(queue.get("completed") or 0),
            "failed": int(queue.get("failed") or 0),
        },
    }


def admin_overview_page(
    request: Request,
    user: Annotated[str, Depends(require_session_user)],
):
    sections = _config_sections()
    page, _actor_user_id = _admin_page_context(user, "overview", config_sections=sections)
    page.update(
        {
            "judgehost": _judgehost_status_view(),
            "active_system_admin_count": config.auth_service.active_system_admin_count(),
            "admin_config_changed_total": sum(
                int(section["changed_count"])
                for section in sections
                if isinstance(section.get("changed_count"), (int, float))
            ),
            "smtp": config.smtp_config_service.snapshot().__dict__,
            "maintenance_status": config.maintenance_service.snapshot(),
            "artifact_usage": _artifact_usage_view(),
        }
    )
    return template_response(request, "admin_overview.html", page)


def admin_judgehosts_page(
    request: Request,
    user: Annotated[str, Depends(require_session_user)],
):
    sections = _config_sections()
    page, _actor_user_id = _admin_page_context(user, "judgehosts", config_sections=sections)
    page.update(
        {
            "judgehost": _judgehost_status_view(),
            "admin_runtime_controls": _runtime_controls(sections),
        }
    )
    return template_response(request, "admin_judgehosts.html", page)


def admin_users_page(
    request: Request,
    user: Annotated[str, Depends(require_session_user)],
):
    sections = _config_sections()
    page, _actor_user_id = _admin_page_context(user, "users", config_sections=sections)
    query = str(request.query_params.get("query") or "").strip()
    page.update(
        {
            "admin_users_query": query,
            "admin_user_rows": config.auth_service.admin_user_rows(query=query, limit=50),
            "admin_password_csrf_token": issue_password_form_csrf_token("admin-password"),
            "admin_password_iters": int(_C.PASSWORD_HASH_ITERS),
            "admin_password_salt": secrets.token_hex(16),
        }
    )
    return template_response(request, "admin_users.html", page)


def admin_mail_page(
    request: Request,
    user: Annotated[str, Depends(require_session_user)],
):
    sections = _config_sections()
    page, _actor_user_id = _admin_page_context(user, "mail", config_sections=sections)
    page["smtp"] = config.smtp_config_service.snapshot().__dict__
    return template_response(request, "admin_mail.html", page)


def admin_config_index(
    user: Annotated[str, Depends(require_session_user)],
):
    _admin_user_context(user)
    sections = _config_sections()
    if not sections:
        raise HTTPException(status_code=404, detail="no system config categories")
    first_slug = str(sections[0]["slug"])
    return RedirectResponse(f"/admin/config/{quote_plus(first_slug)}", status_code=302)


def admin_config_category_page(
    request: Request,
    user: Annotated[str, Depends(require_session_user)],
    category: str,
):
    sections = _config_sections()
    page, _actor_user_id = _admin_page_context(user, "config", config_sections=sections)
    requested_slug = config.system_config_service.category_slug(category)
    selected_section = next(
        (
            section
            for section in sections
            if isinstance(section.get("slug"), str) and section["slug"] == requested_slug
        ),
        None,
    )
    if selected_section is None:
        raise HTTPException(status_code=404, detail="config category not found")
    selected_rows = selected_section.get("rows")
    if not isinstance(selected_rows, list):
        selected_rows = []
    page.update(
        {
            "config_sections": sections,
            "selected_section": selected_section,
            "selected_rows": selected_rows,
            "selected_slug": requested_slug,
            "selected_changed_count": int(selected_section.get("changed_count") or 0),
            "selected_count": int(selected_section.get("count") or 0),
            "admin_config_changed_total": sum(
                int(section["changed_count"])
                for section in sections
                if isinstance(section.get("changed_count"), (int, float))
            ),
        }
    )
    return template_response(request, "admin_config_category.html", page)


def admin_artifacts_cleanup(user: Annotated[str, Depends(require_session_user)]):
    _ctx, actor_user_id = _admin_user_context(user)
    started = config.maintenance_service.start_cleanup(actor_user_id=actor_user_id)
    if started.accepted or started.reason == "already_running":
        return RedirectResponse("/maintenance", status_code=303, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {"error": started.reason, "busy": dict(started.busy)},
        headers={"Cache-Control": "no-store"},
        status_code=409 if started.reason == "busy" else 500,
    )


def admin_smtp_update(
    user: Annotated[str, Depends(require_session_user)],
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_clear_password: str = Form("0"),
):
    ctx, actor_user_id = _admin_user_context(user)
    try:
        config.smtp_config_service.save_from_form(
            host=smtp_host,
            port=smtp_port,
            username=smtp_username,
            password=smtp_password,
            clear_password=_as_bool_form_value(smtp_clear_password),
            actor_user_id=actor_user_id,
        )
        audit(
            ctx["user"]["id"],
            None,
            "smtp_config.update",
            {
                "host": form_text(smtp_host).strip(),
                "port": form_text(smtp_port).strip(),
                "username": form_text(smtp_username).strip(),
                "password_changed": bool(form_text(smtp_password)),
                "password_cleared": _as_bool_form_value(smtp_clear_password),
            },
        )
        message = "SMTP settings updated"
    except ValueError as exc:
        message = str(exc)
    return redirect_response("/admin/mail", status_code=303, message=message)


def admin_smtp_test(
    user: Annotated[str, Depends(require_session_user)],
    smtp_test_recipient: str = Form(""),
):
    ctx, _actor_user_id = _admin_user_context(user)
    recipient = form_text(smtp_test_recipient).strip()
    try:
        config.smtp_config_service.send_test_email(recipient=recipient)
        audit(ctx["user"]["id"], None, "smtp_config.test_email", {"recipient": recipient, "status": "ok"})
        message = "SMTP test email sent"
    except ValueError as exc:
        audit(ctx["user"]["id"], None, "smtp_config.test_email", {"recipient": recipient, "status": "failed"})
        message = str(exc)
    return redirect_response("/admin/mail", status_code=303, message=message)


def admin_judgehost_runtime_update(
    user: Annotated[str, Depends(require_session_user)],
    judgehost_enable: str = Form("0"),
    judgehost_api_token: str = Form(""),
    judgehost_api_username: str = Form(""),
):
    ctx, actor_user_id = _admin_user_context(user)
    message = "Judgehost settings updated"
    try:
        payload = {
            "JUDGEHOST_ENABLE": _as_bool_form_value(judgehost_enable),
            "JUDGEHOST_API_TOKEN": form_text(judgehost_api_token).strip(),
            "JUDGEHOST_API_USERNAME": form_text(judgehost_api_username).strip(),
        }
        result = config.system_config_service.apply_patch(payload, actor_user_id=actor_user_id)
        config.reload_runtime_values()
        changed = int(result.get("changed") or 0)
        diff_rows = result.get("diff")
        diffs = diff_rows if isinstance(diff_rows, list) else []
        restart_changed = sum(
            1 for row in diffs if isinstance(row, dict) and bool(row.get("restart_required"))
        )
        audit(
            ctx["user"]["id"],
            None,
            "system_config.update_judgehost_runtime_controls",
            {"changed_count": changed, "diff": diffs},
        )
        message = "Judgehost settings updated"
        if restart_changed:
            message += ". Restart required."
    except ValueError as exc:
        message = str(exc)
    return redirect_response("/admin/judgehosts", status_code=303, message=message)


def admin_worker_queue_snapshot(
    user: Annotated[str, Depends(require_session_user)],
    limit: int = 200,
):
    _admin_user_context(user)
    cap = coerce_int(limit, 200, 1, 2000)
    payload = config.worker_queue_service.snapshot(limit=cap)
    payload["limit"] = cap
    return JSONResponse(payload)


def admin_judgehost_snapshot(user: Annotated[str, Depends(require_session_user)]):
    _admin_user_context(user)
    return JSONResponse(config.judgehost_task_service.status())


def admin_judgehost_host_action(
    user: Annotated[str, Depends(require_session_user)],
    hostname: str = Form(""),
    action: str = Form(""),
):
    ctx, _actor_user_id = _admin_user_context(user)
    safe_host = form_text(hostname).strip()
    safe_action = form_text(action).strip().lower()
    if not safe_host:
        return redirect_response("/admin/judgehosts", status_code=303, message="Judgehost hostname is required")
    if safe_action not in {"disable", "enable"}:
        return redirect_response("/admin/judgehosts", status_code=303, message="Invalid judgehost action")
    enable_flag = safe_action == "enable"
    try:
        result = config.judgehost_task_service.set_host_enabled(safe_host, enable_flag)
        audit(
            ctx["user"]["id"],
            None,
            "judgehost.host_action",
            {"hostname": safe_host, "action": safe_action, "result": result},
        )
        if enable_flag:
            message = f"Judgehost {safe_host} enabled"
        else:
            message = (
                f"Judgehost {safe_host} disabled; released tasks={int(result.get('released_tasks') or 0)}, "
                f"batches={int(result.get('released_batches') or 0)}, "
                f"cases={int(result.get('released_cases') or 0)}"
            )
    except (RuntimeError, ValueError) as exc:
        message = f"Judgehost action failed: {exc}"
    return redirect_response("/admin/judgehosts", status_code=303, message=message)


async def admin_config_category_update(
    request: Request,
    user: Annotated[str, Depends(require_session_user)],
    category: str,
):
    ctx, actor_user_id = _admin_user_context(user)
    safe_category_slug = config.system_config_service.category_slug(category)
    redirect_target = f"/admin/config/{safe_category_slug}"
    message = "System config updated"
    try:
        config.system_config_service.refresh()
        section = config.system_config_service.section_by_slug(safe_category_slug)
        if section is None:
            raise ValueError("config category not found")
        raw_rows = section.get("rows")
        rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
        if not rows:
            raise ValueError("config category has no editable keys")
        form = await request.form()
        payload: dict[str, object] = {}
        for row in rows:
            key = row.get("key")
            if not isinstance(key, str) or not key:
                continue
            input_name_raw = row.get("input_name")
            input_name = input_name_raw if isinstance(input_name_raw, str) and input_name_raw else f"config_{key}"
            reset_name = f"config_reset_{key}"
            kind_raw = row.get("type")
            kind = kind_raw.lower() if isinstance(kind_raw, str) and kind_raw else "str"
            if reset_name in form:
                payload[key] = row.get("default_value")
            elif kind == "bool":
                payload[key] = input_name in form
            elif input_name in form:
                payload[key] = form.get(input_name)
        result = config.system_config_service.apply_patch(payload, actor_user_id=actor_user_id)
        config.reload_runtime_values()
        changed = int(result.get("changed") or 0)
        raw_diff = result.get("diff")
        diff_rows = raw_diff if isinstance(raw_diff, list) else []
        restart_changed = sum(
            1 for row in diff_rows if isinstance(row, dict) and bool(row.get("restart_required"))
        )
        audit(
            ctx["user"]["id"],
            None,
            "system_config.update_category",
            {"category": safe_category_slug, "changed_count": changed, "diff": diff_rows},
        )
        message = "System configuration updated"
        if restart_changed:
            message += ". Restart required."
    except ValueError as exc:
        message = str(exc)
    return redirect_response(redirect_target, status_code=303, message=message)


def admin_system_config_reset(user: Annotated[str, Depends(require_session_user)]):
    ctx, _actor_user_id = _admin_user_context(user)
    config.system_config_service.reset()
    config.reload_runtime_values()
    audit(ctx["user"]["id"], None, "system_config.reset", {})
    return redirect_response(
        "/admin/config",
        status_code=303,
        message="System configuration reset to defaults",
    )


def _admin_target_username(value: str) -> str:
    return normalize_username_required(form_text(value))


def admin_user_system_admin_update(
    user: Annotated[str, Depends(require_session_user)],
    target_username: str = Form(""),
    action: str = Form("grant"),
):
    _ctx, actor_user_id = _admin_user_context(user)
    try:
        safe_target = _admin_target_username(target_username)
        safe_action = form_text(action).strip().lower()
        if safe_action not in {"grant", "revoke"}:
            raise ValueError("invalid system admin action")
        enabled = safe_action == "grant"
        updated = config.auth_service.set_system_admin(
            actor_user_id=actor_user_id,
            username=safe_target,
            enabled=enabled,
        )
        audit(
            actor_user_id,
            None,
            "system_admin.user_system_admin_update",
            {
                "target_username": str(updated["username"]),
                "is_system_admin": int(updated["is_system_admin"] or 0),
            },
        )
        message = (
            f"{safe_target} is now a system admin"
            if enabled
            else f"{safe_target} is no longer a system admin"
        )
    except ValueError as exc:
        message = str(exc)
    return redirect_response("/admin/users", status_code=303, message=message)


def admin_user_ban_update(
    user: Annotated[str, Depends(require_session_user)],
    target_username: str = Form(""),
    action: str = Form("ban"),
):
    _ctx, actor_user_id = _admin_user_context(user)
    try:
        safe_target = _admin_target_username(target_username)
        safe_action = form_text(action).strip().lower()
        if safe_action not in {"ban", "unban"}:
            raise ValueError("invalid ban action")
        banned = safe_action == "ban"
        updated = config.auth_service.set_user_banned(
            actor_user_id=actor_user_id,
            username=safe_target,
            banned=banned,
        )
        audit(
            actor_user_id,
            None,
            "system_admin.user_ban_update",
            {
                "target_username": str(updated["username"]),
                "is_banned": int(updated["is_banned"] or 0),
            },
        )
        message = f"{safe_target} has been banned" if banned else f"{safe_target} has been unbanned"
    except ValueError as exc:
        message = str(exc)
    return redirect_response("/admin/users", status_code=303, message=message)


def admin_user_password_update(
    user: Annotated[str, Depends(require_session_user)],
    target_username: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    new_password_key_id: str = Form(""),
    new_password_envelope_token: str = Form(""),
    new_password_encrypted_verifier: str = Form(""),
    csrf_token: str = Form(""),
    new_password_salt: str = Form(""),
    new_password_iters: str = Form(""),
):
    _ctx, actor_user_id = _admin_user_context(user)
    _ = (new_password, new_password_confirm)
    try:
        safe_target = _admin_target_username(target_username)
        if safe_target.lower() == form_text(user).strip().lower():
            raise ValueError("use the account password form to change your own password")
        target_row = lookup_user_auth(safe_target)
        if target_row is None:
            raise ValueError(f"user {safe_target} not found")
        password_csrf = form_text(csrf_token).strip()
        if not verify_password_form_csrf_token(password_csrf, "admin-password"):
            raise ValueError("invalid password token")
        try:
            new_verifier = password_envelope_store.consume(
                scope="admin-password",
                purpose="admin-new",
                username=safe_target,
                csrf_token=password_csrf,
                key_id=form_text(new_password_key_id),
                envelope_token=form_text(new_password_envelope_token),
                encrypted_verifier=form_text(new_password_encrypted_verifier),
            )
        except ValueError as exc:
            raise ValueError("invalid new password envelope") from exc
        new_salt = normalize_password_salt_hex(form_text(new_password_salt))
        new_iters = normalize_password_iters(form_text(new_password_iters))
        if new_iters != int(_C.PASSWORD_HASH_ITERS):
            raise ValueError("invalid password iterations")
        set_user_password_verifier(int(target_row["id"]), new_verifier, new_salt, new_iters)
        config.auth_service.revoke_all_access_for_user(int(target_row["id"]))
        audit(
            actor_user_id,
            None,
            "system_admin.user_password_update",
            {"target_username": safe_target, "target_user_id": int(target_row["id"])},
        )
        message = f"Password updated for {safe_target}"
    except ValueError as exc:
        message = str(exc)
    return redirect_response("/admin/users", status_code=303, message=message)
