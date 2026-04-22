from __future__ import annotations
from app.impl.auth.session import require_session_user
from typing import Annotated

import secrets
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, RedirectResponse

from app.impl.auth.session import create_session_for_user, revoke_sudo_sessions_for_user
from app.impl.auth.shared import dummy_password_salt_hex, lookup_user_auth, normalize_password_iters, normalize_password_salt_hex, normalize_password_verifier_hex, redirect_response, set_user_password_verifier, template_response
from app.impl.auth.csrf import issue_password_form_csrf_token, password_proof_from_verifier, verify_password_form_csrf_token
from app.impl.runtime.config import config
from app.impl.problem.shared import _as_bool_form_value, _settings_user_ctx, _system_config_row_by_key
from app.impl.workspace.context_operation import audit, user_participating_problems
from app.impl.workspace.access import is_system_admin_user_id, require_system_admin
from app.main_util import form_text
from app.service.verification.runtime import coerce_int

_C = config.constants


def settings_page(request: Request, user: Annotated[str, Depends(require_session_user)]):
    ctx = _settings_user_ctx(user)
    user_row = dict(ctx['user'])
    is_system_admin = is_system_admin_user_id(int(user_row['id']))
    user_row['is_system_admin'] = 1 if is_system_admin else 0
    problems = user_participating_problems(int(user_row['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    auth_row = lookup_user_auth(str(user_row['username']))
    current_salt = str(auth_row['password_salt'] or '').strip().lower() if auth_row is not None else ''
    try:
        current_iters = int(auth_row['password_iters'] or 0) if auth_row is not None else int(_C.PASSWORD_HASH_ITERS)
    except Exception:
        current_iters = int(_C.PASSWORD_HASH_ITERS)
    if not _C.HEX_32_RE.fullmatch(current_salt):
        current_salt = dummy_password_salt_hex(str(user_row['username']))
    if current_iters <= 0:
        current_iters = int(_C.PASSWORD_HASH_ITERS)
    admin_sections: list[dict[str, object]] = []
    admin_changed_total = 0
    judgehost_status: dict[str, object] = {}
    admin_runtime_controls: dict[str, dict[str, object]] = {}
    admin_default_category_slug = ""
    smtp_config: dict[str, object] = {}
    default_problem = default_problem if isinstance(default_problem := ctx.get('default_problem'), str) else ''
    if (not default_problem) and problems:
        first_problem_slug = problems[0].get('slug')
        default_problem = first_problem_slug if isinstance(first_problem_slug, str) else ''
    if is_system_admin:
        config.system_config_service.refresh()
        admin_sections = config.system_config_service.ui_sections()
        for section in admin_sections:
            category_slug = section['slug'] if isinstance(section.get('slug'), str) else ''
            section['href'] = f"/settings/config/{quote_plus(category_slug)}"
        admin_changed_total = sum((int(section['changed_count']) for section in admin_sections if isinstance(section.get('changed_count'), (int, float))))
        if admin_sections:
            admin_default_category_slug = admin_sections[0]['slug'] if isinstance(admin_sections[0].get('slug'), str) else ''
        rows_by_key = _system_config_row_by_key(admin_sections)
        for key in ("JUDGEHOST_ENABLE", "JUDGEHOST_API_TOKEN", "JUDGEHOST_API_USERNAME"):
            row = rows_by_key.get(key, {})
            description = row['description'] if isinstance(row.get("description"), str) else None
            choices = list(row["choices"]) if isinstance(row.get("choices"), list) else []
            current_value = row.get("current_value")
            current_display = current_display if isinstance(current_display := row.get("current_display"), str) else current_value if isinstance(current_value, str) else None
            impact = row['impact'] if isinstance(row.get("impact"), str) else None
            admin_runtime_controls[key] = {
                "key": key,
                "description": description,
                "choices": choices,
                "current_value": row.get("current_value"),
                "current_display": current_display,
                "changed": bool(row.get("changed")),
                "impact": impact,
            }
        judgehost_status = config.judgehost_task_service.status()
        smtp_config = config.smtp_config_service.snapshot().__dict__
    return template_response(
        request,
        'settings.html',
        {
            'user': user_row,
            'default_problem': default_problem,
            'active_main': 'settings',
            'problems': problems,
            'password_csrf_token': issue_password_form_csrf_token('settings-password'),
            'current_password_salt': current_salt,
            'current_password_iters': current_iters,
            'new_password_salt': secrets.token_hex(16),
            'new_password_iters': int(_C.PASSWORD_HASH_ITERS),
            'is_system_admin': is_system_admin,
            'admin_config_sections': admin_sections,
            'admin_config_changed_total': admin_changed_total,
            'admin_default_category_slug': admin_default_category_slug,
            'judgehost_status': judgehost_status,
            'admin_runtime_controls': admin_runtime_controls,
            'smtp_config': smtp_config,
        },
    )

def settings_smtp_update(
    user: Annotated[str, Depends(require_session_user)],
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_clear_password: str = Form("0"),
):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    redirect_target = "/settings"
    try:
        config.smtp_config_service.save_from_form(
            host=smtp_host,
            port=smtp_port,
            username=smtp_username,
            password=smtp_password,
            clear_password=_as_bool_form_value(smtp_clear_password),
            actor_user_id=int(ctx["user"]["id"]),
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
        msg = "SMTP settings updated"
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(redirect_target, status_code=303, message=msg)

def settings_smtp_test(
    user: Annotated[str, Depends(require_session_user)],
    smtp_test_recipient: str = Form(""),
):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    redirect_target = "/settings"
    try:
        config.smtp_config_service.send_test_email(recipient=smtp_test_recipient)
        audit(
            ctx["user"]["id"],
            None,
            "smtp_config.test_email",
            {"recipient": form_text(smtp_test_recipient).strip(), "status": "ok"},
        )
        msg = "SMTP test email sent"
    except ValueError as exc:
        audit(
            ctx["user"]["id"],
            None,
            "smtp_config.test_email",
            {"recipient": form_text(smtp_test_recipient).strip(), "status": "failed"},
        )
        msg = str(exc)
    return redirect_response(redirect_target, status_code=303, message=msg)

def settings_judgehost_runtime_update(
    user: Annotated[str, Depends(require_session_user)],
    judgehost_enable: str = Form("0"),
    judgehost_api_token: str = Form(""),
    judgehost_api_username: str = Form(""),
):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    redirect_target = "/settings"
    msg = "judgehost runtime settings updated"
    try:
        payload = {
            "JUDGEHOST_ENABLE": _as_bool_form_value(judgehost_enable),
            "JUDGEHOST_API_TOKEN": form_text(judgehost_api_token).strip(),
            "JUDGEHOST_API_USERNAME": form_text(judgehost_api_username).strip(),
        }
        result = config.system_config_service.apply_patch(payload, actor_user_id=int(ctx["user"]["id"]))
        config.reload_runtime_values()
        changed = int(changed) if isinstance(changed := result.get("changed"), (int, float)) else 0
        diff_rows = result.get("diff") if isinstance(result.get("diff"), list) else []
        runtime_changed = sum((1 for row in diff_rows if isinstance(row, dict) and (not bool(row.get("restart_required")))))
        restart_changed = sum((1 for row in diff_rows if isinstance(row, dict) and bool(row.get("restart_required"))))
        audit(
            ctx["user"]["id"],
            None,
            "system_config.update_judgehost_runtime_controls",
            {"changed_count": changed, "diff": diff_rows},
        )
        msg = f"judgehost runtime settings updated ({changed} changes; runtime={runtime_changed}, restart={restart_changed})"
        if restart_changed > 0:
            msg += "; restart required for restart-marked keys"
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(redirect_target, status_code=303, message=msg)

def settings_worker_queue_snapshot(user: Annotated[str, Depends(require_session_user)], limit: int=200):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    cap = coerce_int(limit, 200, 1, 2000)
    payload = config.worker_queue_service.snapshot(limit=cap)
    payload['limit'] = cap
    return JSONResponse(payload)

def settings_judgehost_snapshot(user: Annotated[str, Depends(require_session_user)]):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    payload = config.judgehost_task_service.status()
    if not isinstance(payload, dict):
        payload = {}
    return JSONResponse(dict(payload))

def settings_judgehost_host_action(
    user: Annotated[str, Depends(require_session_user)],
    hostname: str = Form(""),
    action: str = Form(""),
):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    safe_host = str(hostname or "").strip()
    safe_action = str(action or "").strip().lower()
    redirect_target = "/settings"
    if not safe_host:
        return redirect_response(redirect_target, status_code=303, message="judgehost hostname is required")
    if safe_action not in {"disable", "enable"}:
        return redirect_response(redirect_target, status_code=303, message="invalid judgehost action")
    enable_flag = safe_action == "enable"
    try:
        result = config.judgehost_task_service.set_host_enabled(safe_host, enable_flag)
        audit(
            ctx["user"]["id"],
            None,
            "judgehost.host_action",
            {
                "hostname": safe_host,
                "action": safe_action,
                "result": result,
            },
        )
        if enable_flag:
            msg = f"judgehost {safe_host} enabled"
        else:
            released_tasks = int(released_tasks) if isinstance(released_tasks := result.get('released_tasks'), (int, float)) else 0
            released_jobs = int(released_jobs) if isinstance(released_jobs := result.get('released_jobs'), (int, float)) else 0
            released_cases = int(released_cases) if isinstance(released_cases := result.get('released_cases'), (int, float)) else 0
            msg = (
                f"judgehost {safe_host} disabled; released tasks={released_tasks}, "
                f"jobs={released_jobs}, cases={released_cases}"
            )
    except Exception as exc:
        msg = f"judgehost action failed: {exc}"
    return redirect_response(redirect_target, status_code=303, message=msg)

def settings_config_category_page(request: Request, user: Annotated[str, Depends(require_session_user)], category: str):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    user_row = dict(ctx['user'])
    config.system_config_service.refresh()
    sections = config.system_config_service.ui_sections()
    for section in sections:
        category_slug = section['slug'] if isinstance(section.get('slug'), str) else ''
        section['href'] = f"/settings/config/{quote_plus(category_slug)}"
    requested_slug = config.system_config_service.category_slug(category)
    selected_section = None
    for section in sections:
        if isinstance(section.get('slug'), str) and section['slug'] == requested_slug:
            selected_section = section
            break
    if selected_section is None:
        raise HTTPException(status_code=404, detail='config category not found')
    selected_rows = selected_section.get('rows') if isinstance(selected_section, dict) else []
    if not isinstance(selected_rows, list):
        selected_rows = []
    selected_changed = int(selected_changed) if isinstance(selected_changed := selected_section.get('changed_count') if isinstance(selected_section, dict) else None, (int, float)) else 0
    selected_count = int(selected_count) if isinstance(selected_count := selected_section.get('count') if isinstance(selected_section, dict) else None, (int, float)) else 0
    return template_response(
        request,
        'settings_config_category.html',
        {
            'user': user_row,
            'active_main': 'settings',
            'is_system_admin': True,
            'config_sections': sections,
            'selected_section': selected_section,
            'selected_rows': selected_rows,
            'selected_slug': requested_slug,
            'selected_changed_count': selected_changed,
            'selected_count': selected_count,
            'admin_config_changed_total': sum((int(section['changed_count']) for section in sections if isinstance(section.get('changed_count'), (int, float)))),
        },
    )

async def settings_config_category_update(request: Request, user: Annotated[str, Depends(require_session_user)], category: str):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    safe_category_slug = config.system_config_service.category_slug(category)
    redirect_target = f'/settings/config/{safe_category_slug}'
    msg = 'system config updated'
    try:
        config.system_config_service.refresh()
        section = config.system_config_service.section_by_slug(safe_category_slug)
        if section is None:
            raise ValueError('config category not found')
        rows_raw = section.get('rows') if isinstance(section, dict) else []
        if not isinstance(rows_raw, list):
            rows_raw = []
        rows = [row for row in rows_raw if isinstance(row, dict)]
        if not rows:
            raise ValueError('config category has no editable keys')
        form = await request.form()
        payload: dict[str, object] = {}
        for row in rows:
            key = key.strip() if isinstance(key := row.get('key'), str) else ''
            input_name = input_name.strip() if isinstance(input_name := row.get('input_name'), str) and input_name.strip() else f'config_{key}'
            reset_name = f'config_reset_{key}'
            kind = kind.strip().lower() if isinstance(kind := row.get('type'), str) and kind.strip() else 'str'
            if not key:
                continue
            if reset_name in form:
                payload[key] = row.get('default_value')
                continue
            if kind == 'bool':
                payload[key] = bool(input_name in form)
                continue
            if input_name not in form:
                continue
            payload[key] = form.get(input_name)
        result = config.system_config_service.apply_patch(payload, actor_user_id=int(ctx['user']['id']))
        config.reload_runtime_values()
        changed = int(changed) if isinstance(changed := result.get('changed'), (int, float)) else 0
        diff_rows = result.get('diff') if isinstance(result.get('diff'), list) else []
        runtime_changed = sum((1 for row in diff_rows if isinstance(row, dict) and (not bool(row.get('restart_required')))))
        restart_changed = sum((1 for row in diff_rows if isinstance(row, dict) and bool(row.get('restart_required'))))
        audit(
            ctx['user']['id'],
            None,
            'system_config.update_category',
            {'category': safe_category_slug, 'changed_count': changed, 'diff': result.get('diff')},
        )
        msg = f'system config updated ({changed} changes; runtime={runtime_changed}, restart={restart_changed})'
        if restart_changed > 0:
            msg += '; restart required for restart-marked keys'
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(redirect_target, status_code=303, message=msg)

def settings_system_config_reset(user: Annotated[str, Depends(require_session_user)]):
    ctx = _settings_user_ctx(user)
    require_system_admin(ctx)
    config.system_config_service.reset()
    config.reload_runtime_values()
    audit(ctx['user']['id'], None, 'system_config.reset', {})
    return redirect_response('/settings', status_code=303, message='system config reset to defaults; runtime keys reloaded, restart-marked keys need restart')

def settings_password_update(user: Annotated[str, Depends(require_session_user)], current_password: str=Form(''), new_password: str=Form(''), new_password_confirm: str=Form(''), current_password_proof: str=Form(''), new_password_verifier: str=Form(''), new_password_proof: str=Form(''), csrf_token: str=Form(''), new_password_salt: str=Form(''), new_password_iters: str=Form('')):
    row = lookup_user_auth(user)
    msg = 'password updated'
    response: RedirectResponse
    _ = (current_password, new_password, new_password_confirm)
    if row is None:
        msg = 'user not found'
        return redirect_response('/settings', status_code=303, message=msg)
    try:
        proof_token = form_text(csrf_token).strip()
        current_proof_value = form_text(current_password_proof).strip().lower()
        new_verifier_value = form_text(new_password_verifier).strip().lower()
        new_proof_value = form_text(new_password_proof).strip().lower()
        new_salt_value = form_text(new_password_salt)
        new_iters_value = form_text(new_password_iters)
        if not verify_password_form_csrf_token(proof_token, 'settings-password'):
            raise ValueError('invalid password token')
        stored_verifier = str(row['password_hash'] or '').strip().lower()
        if not _C.HEX_64_RE.fullmatch(stored_verifier):
            raise ValueError('current password is incorrect')
        if not _C.HEX_64_RE.fullmatch(current_proof_value):
            raise ValueError('current password is incorrect')
        expected_current_proof = password_proof_from_verifier(proof_token, stored_verifier)
        if not secrets.compare_digest(expected_current_proof, current_proof_value):
            raise ValueError('current password is incorrect')
        new_verifier = normalize_password_verifier_hex(new_verifier_value)
        if not _C.HEX_64_RE.fullmatch(new_proof_value):
            raise ValueError('invalid new password proof')
        new_salt = normalize_password_salt_hex(new_salt_value)
        new_iters = normalize_password_iters(new_iters_value)
        if new_iters != int(_C.PASSWORD_HASH_ITERS):
            raise ValueError('invalid password iterations')
        expected_new_proof = password_proof_from_verifier(proof_token, new_verifier)
        if not secrets.compare_digest(expected_new_proof, new_proof_value):
            raise ValueError('invalid new password proof')
        set_user_password_verifier(int(row['id']), new_verifier, new_salt, new_iters)
        config.auth_service.revoke_auth_sessions_for_user(int(row["id"]))
        revoke_sudo_sessions_for_user(int(row['id']))
        token = create_session_for_user(int(row['id']))
        response = redirect_response('/settings', status_code=303, message=msg)
        response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
        return response
    except ValueError as exc:
        msg = str(exc)
    return redirect_response('/settings', status_code=303, message=msg)
