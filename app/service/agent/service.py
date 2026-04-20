from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import DB, now_iso
from app.runtime_value import RuntimeValues
from app.service.agent.store import AgentStore
from app.service.platform.hashing import canonical_json, sha256_hex_text
from app.service.repository.workspace import WorkspaceService


_SCOPE_LEVELS = {"readonly": 1, "workspace": 2, "commit": 3}
_ROLE_LEVELS = {"read": 1, "write": 3, "owner": 3}
_DEFAULT_REGISTER_TTL_SEC = 900
_DEFAULT_REQUEST_TTL_SEC = 900
_DEFAULT_TOKEN_TTL_SEC = 86400
_ALLOWED_APPROVAL_TTLS = {3600, 86400, 604800, 2592000}


@dataclass(frozen=True)
class AgentTokenIdentity:
    token_id: str
    agent_session_id: str
    user_id: int
    username: str
    problem_id: int
    problem_slug: str
    scope: str
    effective_scope: str
    created_at: str
    expires_at: str


class AgentService:
    def __init__(self, db: DB, workspace_service: WorkspaceService, *, constants: RuntimeValues):
        self.db = db
        self.workspace_service = workspace_service
        self._store = AgentStore(db)
        self.apply_runtime_values(constants)

    def apply_runtime_values(self, constants: RuntimeValues) -> None:
        self._constants = constants

    @staticmethod
    def _parse_iso_utc(raw: str) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except Exception:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _identity_payload(agent_name: str, desktop_id: str, init_ts: str) -> dict[str, str]:
        return {
            "agent_name": str(agent_name or "").strip(),
            "desktop_id": str(desktop_id or "").strip(),
            "init_ts": str(init_ts or "").strip(),
        }

    def _identity_hash(self, *, agent_name: str, desktop_id: str, init_ts: str) -> str:
        payload = self._identity_payload(agent_name, desktop_id, init_ts)
        if not payload["agent_name"] or not payload["desktop_id"] or not payload["init_ts"]:
            raise ValueError("identity payload is incomplete")
        return sha256_hex_text(canonical_json(payload, ensure_ascii=False))

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        safe_scope = str(scope or "").strip().lower()
        if safe_scope not in _SCOPE_LEVELS:
            raise ValueError("invalid scope")
        return safe_scope

    @staticmethod
    def _scope_allows(granted_scope: str, min_scope: str) -> bool:
        return _SCOPE_LEVELS.get(str(granted_scope or ""), 0) >= _SCOPE_LEVELS.get(str(min_scope or ""), 0)

    @staticmethod
    def _effective_scope(token_scope: str, acl_role: str) -> str:
        token_level = _SCOPE_LEVELS.get(str(token_scope or ""), 0)
        acl_level = _ROLE_LEVELS.get(str(acl_role or ""), 0)
        level = min(token_level, acl_level)
        if level >= _SCOPE_LEVELS["commit"]:
            return "commit"
        if level >= _SCOPE_LEVELS["workspace"]:
            return "workspace"
        if level >= _SCOPE_LEVELS["readonly"]:
            return "readonly"
        return ""

    @staticmethod
    def _format_expiry(seconds: int | None) -> str | None:
        if seconds is None:
            return None
        return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat()

    @staticmethod
    def _require_problem_slug(problem: str) -> str:
        safe_problem = str(problem or "").strip()
        if not safe_problem:
            raise ValueError("problem is required")
        return safe_problem

    def _problem_acl_role(self, *, problem_id: int, user_id: int) -> str:
        access = self.workspace_service.access_context(int(problem_id), int(user_id))
        return str(access.get("role") or "none")

    def _require_active_session(self, *, agent_session_id: str, identity_hash: str) -> dict[str, object]:
        session = self._store.session_by_id(agent_session_id)
        if session is None or str(session.get("revoked_at") or ""):
            raise PermissionError("agent session is invalid")
        if str(session.get("identity_hash") or "") != str(identity_hash or ""):
            raise PermissionError("agent identity mismatch")
        return dict(session)

    def _touch_session(self, session_id: str, *, last_seen_at: str | None = None) -> str:
        now_text = now_iso() if last_seen_at is None else last_seen_at
        self._store.touch_session(session_id, last_seen_at=now_text)
        return now_text

    def _authorized_problems_for_session(self, *, session_id: str, user_id: int) -> list[dict[str, object]]:
        now_dt = datetime.now(timezone.utc)
        merged: dict[str, dict[str, object]] = {}
        for token_row in self._store.list_session_tokens(session_id):
            if str(token_row.get("revoked_at") or ""):
                continue
            expires_at_text = str(token_row.get("expires_at") or "")
            expires_at = self._parse_iso_utc(expires_at_text)
            if expires_at is not None and expires_at <= now_dt:
                continue
            effective_scope = self._effective_scope(
                str(token_row.get("scope") or ""),
                self._problem_acl_role(problem_id=int(token_row["problem_id"]), user_id=int(user_id)),
            )
            if not effective_scope:
                continue
            problem_slug = str(token_row["problem_slug"] or "")
            candidate = {
                "problem": problem_slug,
                "scope": effective_scope,
                "expires_at": expires_at_text or None,
                "created_at": str(token_row.get("created_at") or ""),
            }
            existing = merged.get(problem_slug)
            if existing is None:
                merged[problem_slug] = candidate
                continue
            candidate_scope_level = _SCOPE_LEVELS.get(str(candidate["scope"]), 0)
            existing_scope_level = _SCOPE_LEVELS.get(str(existing["scope"]), 0)
            if candidate_scope_level > existing_scope_level:
                merged[problem_slug] = candidate
                continue
            if candidate_scope_level < existing_scope_level:
                continue
            candidate_expires = str(candidate.get("expires_at") or "")
            existing_expires = str(existing.get("expires_at") or "")
            if not existing_expires and candidate_expires:
                continue
            if not candidate_expires and existing_expires:
                merged[problem_slug] = candidate
                continue
            if candidate_expires > existing_expires:
                merged[problem_slug] = candidate
                continue
            if candidate_expires == existing_expires and str(candidate["created_at"]) > str(existing["created_at"]):
                merged[problem_slug] = candidate
        items = [
            {
                "problem": str(item["problem"] or ""),
                "scope": str(item["scope"] or ""),
                "expires_at": item["expires_at"],
            }
            for item in merged.values()
        ]
        items.sort(key=lambda item: str(item["problem"] or ""))
        return items

    def create_registration_code(self, *, user_id: int, ttl_sec: int = _DEFAULT_REGISTER_TTL_SEC) -> dict[str, object]:
        safe_ttl = max(60, min(3600, int(ttl_sec)))
        code = f"reg-{secrets.token_hex(8)}"
        expires_at = self._format_expiry(safe_ttl)
        if expires_at is None:
            raise RuntimeError("registration expiry required")
        self._store.create_registration_code(code=code, user_id=int(user_id), expires_at=expires_at)
        self.workspace_service.record_audit_event(
            actor_user_id=int(user_id),
            problem_id=None,
            action="agent.connect.create_registration_code",
            details={"code": code, "expires_at": expires_at},
        )
        return {"code": code, "expires_at": expires_at, "expires_in": safe_ttl}

    def register_agent(self, *, code: str, agent_name: str, desktop_id: str, init_ts: str) -> dict[str, object]:
        now_text = now_iso()
        claimed = self._store.claim_registration_code(str(code or "").strip(), now_text=now_text)
        if claimed is None:
            raise LookupError("registration code not found")
        if str(claimed.get("used_at") or "") and str(claimed.get("used_at") or "") != now_text:
            raise RuntimeError("registration code already used")
        expires_at = self._parse_iso_utc(str(claimed.get("expires_at") or ""))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise TimeoutError("registration code expired")
        identity_hash = self._identity_hash(agent_name=agent_name, desktop_id=desktop_id, init_ts=init_ts)
        existing = self._store.active_session_by_identity(user_id=int(claimed["user_id"]), identity_hash=identity_hash)
        if existing is not None:
            self._store.touch_session(existing["id"], last_seen_at=now_text)
            self.workspace_service.record_audit_event(
                actor_user_id=int(claimed["user_id"]),
                problem_id=None,
                action="agent.session.reuse",
                details={
                    "agent_session_id": existing["id"],
                    "agent_name": existing["agent_name"],
                    "desktop_id": existing["desktop_id"],
                    "identity_hash": identity_hash,
                },
            )
            return {
                "agent_session_id": existing["id"],
                "user": existing["username"],
                "server_name": "Polygon Replica",
                "identity_hash": identity_hash,
            }
        session_id = f"as-{secrets.token_hex(8)}"
        self._store.insert_session(
            session_id=session_id,
            user_id=int(claimed["user_id"]),
            identity_hash=identity_hash,
            agent_name=str(agent_name or "").strip(),
            desktop_id=str(desktop_id or "").strip(),
            init_ts=str(init_ts or "").strip(),
            created_at=now_text,
        )
        self.workspace_service.record_audit_event(
            actor_user_id=int(claimed["user_id"]),
            problem_id=None,
            action="agent.session.connect",
            details={
                "agent_session_id": session_id,
                "agent_name": str(agent_name or "").strip(),
                "desktop_id": str(desktop_id or "").strip(),
                "identity_hash": identity_hash,
            },
        )
        return {
            "agent_session_id": session_id,
            "user": str(claimed["username"] or ""),
            "server_name": "Polygon Replica",
            "identity_hash": identity_hash,
        }

    def request_problem_access(
        self,
        *,
        agent_session_id: str,
        identity_hash: str,
        problem: str,
        ttl_sec: int = _DEFAULT_REQUEST_TTL_SEC,
    ) -> dict[str, object]:
        session = self._require_active_session(agent_session_id=agent_session_id, identity_hash=identity_hash)
        safe_problem = self._require_problem_slug(problem)
        problem_id = self.workspace_service.known_problem_id(safe_problem)
        if problem_id is None:
            raise LookupError("problem not found")
        access = self.workspace_service.access_context(int(problem_id), int(session["user_id"]))
        if not bool(access.get("can_read")):
            raise LookupError("problem not found")
        safe_ttl = max(60, min(3600, int(ttl_sec)))
        request_id = f"ar-{secrets.token_hex(8)}"
        expires_at = self._format_expiry(safe_ttl)
        if expires_at is None:
            raise RuntimeError("request expiry required")
        self._store.create_access_request(
            request_id=request_id,
            agent_session_id=session["id"],
            problem_id=int(problem_id),
            expires_at=expires_at,
        )
        self.workspace_service.record_audit_event(
            actor_user_id=int(session["user_id"]),
            problem_id=int(problem_id),
            action="agent.access_request.create",
            details={
                "request_id": request_id,
                "agent_session_id": session["id"],
                "problem": safe_problem,
                "expires_at": expires_at,
            },
        )
        self._touch_session(str(session["id"]))
        return {"request_id": request_id, "approve_path": f"/agent/approve/{request_id}", "expires_in": safe_ttl}

    def poll_access_request(self, *, agent_session_id: str, identity_hash: str, request_id: str) -> dict[str, object]:
        session = self._require_active_session(agent_session_id=agent_session_id, identity_hash=identity_hash)
        row = self._store.access_request_by_id(request_id)
        if row is None or row["agent_session_id"] != session["id"]:
            raise LookupError("access request not found")
        now_dt = datetime.now(timezone.utc)
        expires_at = self._parse_iso_utc(row["expires_at"])
        if row["status"] == "pending" and (expires_at is None or expires_at <= now_dt):
            self._store.resolve_access_request(request_id=row["id"], status="expired", resolved_at=now_iso())
            row = self._store.access_request_by_id(request_id)
            if row is None:
                self._touch_session(str(session["id"]))
                return {"status": "expired"}
        status = str(row["status"] or "pending")
        if status != "approved":
            self._touch_session(str(session["id"]))
            return {"status": status}
        token = str(row.get("delivery_token") or "")
        if token and not str(row.get("delivered_at") or ""):
            self._store.mark_request_delivered(row["id"], delivered_at=now_iso())
            self._touch_session(str(session["id"]))
            return {
                "status": "approved",
                "token": token,
                "problem": row["problem_slug"],
                "expires_at": self._token_expires_at(row.get("token_id") or ""),
            }
        self._touch_session(str(session["id"]))
        return {
            "status": "approved",
            "problem": row["problem_slug"],
            "expires_at": self._token_expires_at(row.get("token_id") or ""),
        }

    def session_status(self, *, agent_session_id: str, identity_hash: str) -> dict[str, object]:
        session = self._require_active_session(agent_session_id=agent_session_id, identity_hash=identity_hash)
        last_seen_at = self._touch_session(str(session["id"]))
        return {
            "status": "ok",
            "agent_session_id": str(session["id"] or ""),
            "identity_hash": str(session["identity_hash"] or ""),
            "user": str(session["username"] or ""),
            "server_name": "Polygon Replica",
            "last_seen_at": last_seen_at,
            "authorized_problems": self._authorized_problems_for_session(
                session_id=str(session["id"] or ""),
                user_id=int(session["user_id"]),
            ),
        }

    def _token_expires_at(self, token_id: str) -> str | None:
        if not token_id:
            return None
        row = self._store.token_by_id(str(token_id or ""))
        if row is None:
            return None
        expires_at = str(row["expires_at"] or "")
        return expires_at or None

    def resolve_access_request(
        self,
        *,
        actor_user_id: int,
        request_id: str,
        decision: str,
        scope: str,
        ttl: str,
    ) -> dict[str, object]:
        row = self._store.access_request_by_id(request_id)
        if row is None:
            raise LookupError("access request not found")
        if int(row["user_id"]) != int(actor_user_id):
            raise LookupError("access request not found")
        if str(row.get("status") or "") != "pending":
            raise ValueError("access request is no longer pending")
        expires_at = self._parse_iso_utc(row["expires_at"])
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            self._store.resolve_access_request(request_id=row["id"], status="expired", resolved_at=now_iso())
            raise TimeoutError("access request expired")
        safe_decision = str(decision or "").strip().lower()
        if safe_decision not in {"approve", "deny"}:
            raise ValueError("invalid decision")
        if safe_decision == "deny":
            self._store.resolve_access_request(request_id=row["id"], status="denied", resolved_at=now_iso())
            self.workspace_service.record_audit_event(
                actor_user_id=int(actor_user_id),
                problem_id=int(row["problem_id"]),
                action="agent.access_request.deny",
                details={"request_id": row["id"], "agent_session_id": row["agent_session_id"]},
            )
            return {"status": "denied"}
        safe_scope = self._normalize_scope(scope)
        ttl_seconds: int | None
        if str(ttl or "").strip().lower() == "forever":
            ttl_seconds = None
        else:
            try:
                ttl_value = int(str(ttl or "").strip())
            except Exception as exc:
                raise ValueError("invalid ttl") from exc
            if ttl_value not in _ALLOWED_APPROVAL_TTLS:
                raise ValueError("invalid ttl")
            ttl_seconds = ttl_value
        token = f"poly_{secrets.token_urlsafe(24)}"
        token_id = f"at-{secrets.token_hex(8)}"
        token_hash = sha256_hex_text(token)
        created_at = now_iso()
        token_expires_at = self._format_expiry(ttl_seconds)
        self._store.insert_token(
            token_id=token_id,
            token_hash=token_hash,
            agent_session_id=row["agent_session_id"],
            user_id=int(row["user_id"]),
            problem_id=int(row["problem_id"]),
            scope=safe_scope,
            created_at=created_at,
            expires_at=token_expires_at,
        )
        self._store.resolve_access_request(
            request_id=row["id"],
            status="approved",
            resolved_at=created_at,
            token_id=token_id,
            delivery_token=token,
        )
        self.workspace_service.record_audit_event(
            actor_user_id=int(actor_user_id),
            problem_id=int(row["problem_id"]),
            action="agent.access_request.approve",
            details={
                "request_id": row["id"],
                "agent_session_id": row["agent_session_id"],
                "token_id": token_id,
                "scope": safe_scope,
                "expires_at": token_expires_at or "",
            },
        )
        self.workspace_service.record_audit_event(
            actor_user_id=int(actor_user_id),
            problem_id=int(row["problem_id"]),
            action="agent.token.mint",
            details={
                "token_id": token_id,
                "agent_session_id": row["agent_session_id"],
                "scope": safe_scope,
                "expires_at": token_expires_at or "",
            },
        )
        return {"status": "approved", "token_id": token_id, "expires_at": token_expires_at}

    def list_user_sessions(self, *, user_id: int) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        now_dt = datetime.now(timezone.utc)
        for session in self._store.list_user_sessions(int(user_id)):
            tokens = []
            for token in self._store.list_session_tokens(session["id"]):
                if str(token.get("revoked_at") or ""):
                    continue
                expires_at = self._parse_iso_utc(str(token.get("expires_at") or ""))
                if expires_at is not None and expires_at <= now_dt:
                    continue
                tokens.append(token)
            items.append(
                {
                    "id": session["id"],
                    "identity_hash": session["identity_hash"],
                    "agent_name": session["agent_name"],
                    "desktop_id": session["desktop_id"],
                    "init_ts": session["init_ts"],
                    "created_at": session["created_at"],
                    "last_seen_at": session["last_seen_at"],
                    "tokens": list(tokens),
                }
            )
        return items

    def access_request_for_user(self, *, actor_user_id: int, request_id: str) -> dict[str, object]:
        row = self._store.access_request_by_id(request_id)
        if row is None or int(row["user_id"]) != int(actor_user_id):
            raise LookupError("access request not found")
        return dict(row)

    def revoke_token(self, *, actor_user_id: int, token_id: str) -> None:
        row = self.db.fetch_one(
            "SELECT problem_id FROM agent_tokens WHERE id=? AND user_id=?",
            [str(token_id or ""), int(actor_user_id)],
        )
        if row is None:
            raise LookupError("token not found")
        updated = self._store.revoke_token(token_id=str(token_id or ""), user_id=int(actor_user_id), revoked_at=now_iso())
        if updated <= 0:
            raise LookupError("token not found")
        self.workspace_service.record_audit_event(
            actor_user_id=int(actor_user_id),
            problem_id=int(row["problem_id"]),
            action="agent.token.revoke",
            details={"token_id": str(token_id or "")},
        )

    def disconnect_session(self, *, actor_user_id: int, session_id: str) -> None:
        row = self._store.session_by_id(session_id)
        if row is None or int(row["user_id"]) != int(actor_user_id):
            raise LookupError("agent session not found")
        deleted = self._store.delete_session_state(session_id=str(session_id or ""), user_id=int(actor_user_id))
        if int(deleted["session_count"]) <= 0:
            raise LookupError("agent session not found")
        self.workspace_service.record_audit_event(
            actor_user_id=int(actor_user_id),
            problem_id=None,
            action="agent.session.disconnect",
            details={
                "agent_session_id": str(session_id or ""),
                "agent_name": str(row["agent_name"] or ""),
                "desktop_id": str(row["desktop_id"] or ""),
                "deleted_token_count": int(deleted["token_count"]),
                "deleted_access_request_count": int(deleted["access_request_count"]),
            },
        )

    def token_identity(self, raw_token: str) -> AgentTokenIdentity | None:
        safe_token = str(raw_token or "").strip()
        if not safe_token:
            return None
        token_row = self._store.token_by_hash(sha256_hex_text(safe_token))
        if token_row is None:
            return None
        if str(token_row.get("revoked_at") or ""):
            return None
        expires_at = self._parse_iso_utc(str(token_row.get("expires_at") or ""))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            self._store.revoke_token(token_id=token_row["id"], user_id=int(token_row["user_id"]), revoked_at=now_iso())
            return None
        session = self._store.session_by_id(token_row["agent_session_id"])
        if session is None or str(session.get("revoked_at") or ""):
            return None
        acl_role = self._problem_acl_role(problem_id=int(token_row["problem_id"]), user_id=int(token_row["user_id"]))
        effective_scope = self._effective_scope(str(token_row["scope"] or ""), acl_role)
        if not effective_scope:
            return None
        return AgentTokenIdentity(
            token_id=token_row["id"],
            agent_session_id=token_row["agent_session_id"],
            user_id=int(token_row["user_id"]),
            username=token_row["username"],
            problem_id=int(token_row["problem_id"]),
            problem_slug=token_row["problem_slug"],
            scope=token_row["scope"],
            effective_scope=effective_scope,
            created_at=token_row["created_at"],
            expires_at=str(token_row.get("expires_at") or ""),
        )

    def require_token(self, raw_token: str, *, min_scope: str) -> AgentTokenIdentity:
        identity = self.token_identity(raw_token)
        if identity is None:
            raise PermissionError("invalid bearer token")
        safe_min_scope = self._normalize_scope(min_scope)
        if not self._scope_allows(identity.effective_scope, safe_min_scope):
            raise RuntimeError("token scope does not allow this operation")
        return identity
