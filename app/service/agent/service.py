import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import DB, now_iso
from app.service.access.model import AgentGeneralScope, AgentScope
from app.service.access.policy import (
    agent_general_scope,
    effective_agent_scope,
    stronger_agent_scope,
)
from app.service.access.query import AccessQuery
from app.service.agent.store import (
    AgentProblemGrantRow,
    AgentSessionRow,
    AgentStore,
)
from app.service.platform.hashing import canonical_json, sha256_hex_text
from app.service.repository.workspace import WorkspaceService


_DEFAULT_REGISTER_TTL_SEC = 900
_DEFAULT_REQUEST_TTL_SEC = 900
_ALLOWED_APPROVAL_TTLS = {3600, 86400, 604800, 2592000}
_AGENT_CREDENTIAL_PREFIX = "polygon_agent_"


@dataclass(frozen=True)
class AgentSessionIdentity:
    agent_session_id: str
    user_id: int
    username: str
    general_scope: AgentGeneralScope


@dataclass(frozen=True)
class AgentProblemIdentity:
    agent_session_id: str
    user_id: int
    username: str
    problem_id: int
    problem_slug: str
    declared_scope: AgentScope
    effective_scope: AgentScope


class AgentPermissionRequired(PermissionError):
    def __init__(self, *, problem: str, required_scope: str):
        super().__init__("agent permission required")
        self.problem = problem
        self.required_scope = required_scope


class AgentGeneralPermissionRequired(PermissionError):
    def __init__(self, *, required_scope: str):
        super().__init__("agent general permission required")
        self.required_scope = required_scope


class AgentService:
    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        access_query: AccessQuery,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.access_query = access_query
        self._store = AgentStore(db)

    @property
    def store(self) -> AgentStore:
        return self._store

    @staticmethod
    def _parse_iso_utc(raw: str) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _identity_payload(
        agent_name: str,
        desktop_id: str,
        init_ts: str,
    ) -> dict[str, str]:
        return {
            "agent_name": str(agent_name or "").strip(),
            "desktop_id": str(desktop_id or "").strip(),
            "init_ts": str(init_ts or "").strip(),
        }

    def _identity_hash(
        self,
        *,
        agent_name: str,
        desktop_id: str,
        init_ts: str,
    ) -> str:
        payload = self._identity_payload(agent_name, desktop_id, init_ts)
        if not payload["agent_name"] or not payload["desktop_id"] or not payload["init_ts"]:
            raise ValueError("identity payload is incomplete")
        return sha256_hex_text(canonical_json(payload, ensure_ascii=False))

    @staticmethod
    def _format_expiry(seconds: int | None) -> str | None:
        if seconds is None:
            return None
        return (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))
        ).isoformat()

    @staticmethod
    def _require_problem_slug(problem: str) -> str:
        safe_problem = str(problem or "").strip()
        if not safe_problem:
            raise ValueError("problem is required")
        return safe_problem

    def _require_active_session(
        self,
        *,
        credential: str,
    ) -> AgentSessionRow:
        supplied = str(credential or "")
        if not supplied.startswith(_AGENT_CREDENTIAL_PREFIX):
            raise PermissionError("agent credential is invalid")
        session = self._store.active_session_by_credential_sha256(
            sha256_hex_text(supplied)
        )
        if session is None or session["revoked_at"]:
            raise PermissionError("agent credential is invalid")
        return session

    def _touch_session(
        self,
        session_id: str,
        *,
        last_seen_at: str | None = None,
    ) -> str:
        now_text = now_iso() if last_seen_at is None else last_seen_at
        self._store.touch_session(session_id, last_seen_at=now_text)
        return now_text

    def session_identity(
        self,
        *,
        credential: str,
    ) -> AgentSessionIdentity:
        session = self._require_active_session(
            credential=credential,
        )
        self._touch_session(session["id"])
        return AgentSessionIdentity(
            agent_session_id=session["id"],
            user_id=session["user_id"],
            username=session["username"],
            general_scope=session["general_scope"],
        )

    def require_general_scope(
        self,
        *,
        credential: str,
        minimum_scope: str,
    ) -> AgentSessionIdentity:
        identity = self.session_identity(
            credential=credential,
        )
        safe_minimum = self.access_query.canonical_agent_scope(minimum_scope)
        if identity.general_scope == "none" or not self.access_query.agent_scope_allows(
            identity.general_scope,
            safe_minimum,
        ):
            raise AgentGeneralPermissionRequired(required_scope=safe_minimum)
        return identity

    def _active_grants(
        self,
        session_id: str,
    ) -> list[AgentProblemGrantRow]:
        now_value = datetime.now(timezone.utc)
        result: list[AgentProblemGrantRow] = []
        for grant in self._store.list_session_grants(session_id):
            if grant["revoked_at"]:
                continue
            expires_at = self._parse_iso_utc(grant["expires_at"])
            if grant["expires_at"] and (
                expires_at is None or expires_at <= now_value
            ):
                continue
            result.append(grant)
        return result

    def _declared_problem_scope(
        self,
        session: AgentSessionIdentity,
        problem_id: int,
        *,
        general_only: bool,
    ) -> AgentGeneralScope:
        declared = session.general_scope
        if general_only:
            return declared
        for grant in self._active_grants(session.agent_session_id):
            if grant["problem_id"] != int(problem_id):
                continue
            if declared == "none":
                declared = grant["scope"]
            else:
                declared = stronger_agent_scope(declared, grant["scope"])
        return declared

    def problem_identity_for_session(
        self,
        session: AgentSessionIdentity,
        *,
        problem: str,
        minimum_scope: str,
        general_only: bool = False,
    ) -> AgentProblemIdentity:
        safe_problem = self._require_problem_slug(problem)
        try:
            problem_id = self.workspace_service.known_problem_id(safe_problem)
        except ValueError as exc:
            raise ValueError("invalid problem") from exc
        if problem_id is None:
            raise LookupError("problem not found")
        problem_access = self.access_query.problem_context(
            int(problem_id),
            session.user_id,
        )
        if not problem_access["can_read"]:
            raise LookupError("problem not found")
        declared_scope = self._declared_problem_scope(
            session,
            int(problem_id),
            general_only=general_only,
        )
        safe_minimum = self.access_query.canonical_agent_scope(minimum_scope)
        if declared_scope == "none":
            raise AgentPermissionRequired(
                problem=safe_problem,
                required_scope=safe_minimum,
            )
        effective_scope = effective_agent_scope(
            declared_scope,
            problem_access["role"],
        )
        if effective_scope is None or not self.access_query.agent_scope_allows(
            effective_scope,
            safe_minimum,
        ):
            raise AgentPermissionRequired(
                problem=safe_problem,
                required_scope=safe_minimum,
            )
        return AgentProblemIdentity(
            agent_session_id=session.agent_session_id,
            user_id=session.user_id,
            username=session.username,
            problem_id=int(problem_id),
            problem_slug=safe_problem,
            declared_scope=declared_scope,
            effective_scope=effective_scope,
        )

    def problem_identity(
        self,
        *,
        credential: str,
        problem: str,
        minimum_scope: str,
    ) -> AgentProblemIdentity:
        session = self.session_identity(
            credential=credential,
        )
        return self.problem_identity_for_session(
            session,
            problem=problem,
            minimum_scope=minimum_scope,
        )

    def create_registration_code(
        self,
        *,
        user_id: int,
        ttl_sec: int = _DEFAULT_REGISTER_TTL_SEC,
    ) -> dict[str, object]:
        safe_ttl = max(60, min(3600, int(ttl_sec)))
        code = f"reg-{secrets.token_hex(8)}"
        expires_at = self._format_expiry(safe_ttl)
        if expires_at is None:
            raise RuntimeError("registration expiry required")
        self._store.create_registration_code(
            code=code,
            user_id=int(user_id),
            expires_at=expires_at,
        )
        return {
            "code": code,
            "expires_at": expires_at,
            "expires_in": safe_ttl,
        }

    def register_agent(
        self,
        *,
        code: str,
        agent_name: str,
        desktop_id: str,
        init_ts: str,
        existing_session_id: str,
    ) -> dict[str, object]:
        now_text = now_iso()
        claimed = self._store.claim_registration_code(
            str(code or "").strip(),
            now_text=now_text,
        )
        if claimed is None:
            raise LookupError("registration code not found")
        if claimed["used_at"] and claimed["used_at"] != now_text:
            raise RuntimeError("registration code already used")
        expires_at = self._parse_iso_utc(claimed["expires_at"])
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise TimeoutError("registration code expired")
        identity_hash = self._identity_hash(
            agent_name=agent_name,
            desktop_id=desktop_id,
            init_ts=init_ts,
        )
        credential = _AGENT_CREDENTIAL_PREFIX + secrets.token_urlsafe(32)
        credential_sha256 = sha256_hex_text(credential)
        if existing_session_id:
            reconnected = self._store.rotate_session_credential(
                session_id=existing_session_id,
                user_id=claimed["user_id"],
                identity_hash=identity_hash,
                credential_sha256=credential_sha256,
                last_seen_at=now_text,
            )
            if not reconnected:
                raise PermissionError("existing agent session does not match")
            existing = self._store.session_by_id(existing_session_id)
            if existing is None:
                raise RuntimeError("reconnected agent session is unavailable")
            return {
                "agent_session_id": existing["id"],
                "user": existing["username"],
                "server_name": "Polygon Replica",
                "credential": credential,
            }
        existing = self._store.active_session_by_identity(
            user_id=claimed["user_id"],
            identity_hash=identity_hash,
        )
        if existing is not None:
            raise PermissionError("existing agent session requires reconnect")
        session_id = f"as-{secrets.token_hex(24)}"
        self._store.insert_session(
            session_id=session_id,
            user_id=claimed["user_id"],
            identity_hash=identity_hash,
            credential_sha256=credential_sha256,
            agent_name=str(agent_name or "").strip(),
            desktop_id=str(desktop_id or "").strip(),
            init_ts=str(init_ts or "").strip(),
            created_at=now_text,
        )
        return {
            "agent_session_id": session_id,
            "user": claimed["username"],
            "server_name": "Polygon Replica",
            "credential": credential,
        }

    def request_problem_access(
        self,
        *,
        session: AgentSessionIdentity,
        problem: str,
        requested_scope: str,
        ttl_sec: int = _DEFAULT_REQUEST_TTL_SEC,
    ) -> dict[str, object]:
        safe_problem = self._require_problem_slug(problem)
        safe_scope = self.access_query.canonical_agent_scope(requested_scope)
        try:
            problem_id = self.workspace_service.known_problem_id(safe_problem)
        except ValueError as exc:
            raise ValueError("invalid problem") from exc
        if problem_id is None:
            raise LookupError("problem not found")
        access = self.access_query.problem_context(problem_id, session.user_id)
        if not access["can_read"]:
            raise LookupError("problem not found")
        possible_scope = effective_agent_scope(safe_scope, access["role"])
        if possible_scope != safe_scope:
            raise PermissionError(
                "current problem access does not allow requested scope"
            )
        now_text = now_iso()
        pending = self._store.pending_access_request(
            agent_session_id=session.agent_session_id,
            problem_id=problem_id,
            requested_scope=safe_scope,
            now_text=now_text,
        )
        safe_ttl = max(60, min(3600, int(ttl_sec)))
        if pending is not None:
            remaining = self._parse_iso_utc(pending["expires_at"])
            remaining_seconds = (
                max(
                    0,
                    int(
                        (remaining - datetime.now(timezone.utc)).total_seconds()
                    ),
                )
                if remaining is not None
                else 0
            )
            return {
                "request_id": pending["id"],
                "approve_path": f"/agent/approve/{pending['id']}",
                "expires_in": remaining_seconds,
                "requested_scope": safe_scope,
            }
        request_id = f"ar-{secrets.token_hex(8)}"
        expires_at = self._format_expiry(safe_ttl)
        if expires_at is None:
            raise RuntimeError("request expiry required")
        self._store.create_access_request(
            request_id=request_id,
            agent_session_id=session.agent_session_id,
            problem_id=problem_id,
            requested_scope=safe_scope,
            expires_at=expires_at,
        )
        return {
            "request_id": request_id,
            "approve_path": f"/agent/approve/{request_id}",
            "expires_in": safe_ttl,
            "requested_scope": safe_scope,
        }

    def poll_access_request(
        self,
        *,
        session: AgentSessionIdentity,
        request_id: str,
    ) -> dict[str, object]:
        row = self._store.access_request_by_id(request_id)
        if row is None or row["agent_session_id"] != session.agent_session_id:
            raise LookupError("access request not found")
        expires_at = self._parse_iso_utc(row["expires_at"])
        if row["status"] == "pending" and (
            expires_at is None or expires_at <= datetime.now(timezone.utc)
        ):
            self._store.resolve_pending_request(
                request_id=row["id"],
                status="expired",
                resolved_at=now_iso(),
            )
            row = self._store.access_request_by_id(request_id)
            if row is None:
                raise LookupError("access request not found")
        result: dict[str, object] = {
            "status": row["status"],
            "problem": row["problem_slug"],
            "requested_scope": row["requested_scope"],
        }
        if row["status"] == "approved":
            result.update(
                {
                    "grant_id": row["grant_id"],
                    "granted_scope": row["granted_scope"],
                    "expires_at": row["grant_expires_at"] or None,
                }
            )
        return result

    def _status_grants(
        self,
        session: AgentSessionIdentity,
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for grant in self._active_grants(session.agent_session_id):
            effective = self.access_query.effective_agent_scope(
                declared_scope=grant["scope"],
                problem_id=grant["problem_id"],
                user_id=session.user_id,
            )
            result.append(
                {
                    "grant_id": grant["id"],
                    "problem": grant["problem_slug"],
                    "scope": grant["scope"],
                    "effective_scope": effective or "none",
                    "created_at": grant["created_at"],
                    "expires_at": grant["expires_at"] or None,
                }
            )
        return result

    def _settings_grants(
        self,
        *,
        session_id: str,
        user_id: int,
    ) -> list[dict[str, object]]:
        now_value = datetime.now(timezone.utc)
        result: list[dict[str, object]] = []
        for grant in self._store.list_session_grants(session_id):
            expires_at = self._parse_iso_utc(grant["expires_at"])
            if grant["revoked_at"]:
                status = "revoked"
            elif grant["expires_at"] and (
                expires_at is None or expires_at <= now_value
            ):
                status = "expired"
            else:
                status = "active"
            effective = None
            if status == "active":
                effective = self.access_query.effective_agent_scope(
                    declared_scope=grant["scope"],
                    problem_id=grant["problem_id"],
                    user_id=user_id,
                )
            result.append(
                {
                    "grant_id": grant["id"],
                    "problem": grant["problem_slug"],
                    "scope": grant["scope"],
                    "effective_scope": effective or "none",
                    "created_at": grant["created_at"],
                    "expires_at": grant["expires_at"] or None,
                    "revoked_at": grant["revoked_at"] or None,
                    "status": status,
                }
            )
        return result

    def session_status(
        self,
        *,
        session: AgentSessionIdentity,
    ) -> dict[str, object]:
        row = self._store.session_by_id(session.agent_session_id)
        if row is None:
            raise PermissionError("agent session is invalid")
        return {
            "status": "ok",
            "agent_session_id": session.agent_session_id,
            "user": session.username,
            "server_name": "Polygon Replica",
            "last_seen_at": row["last_seen_at"],
            "general_scope": session.general_scope,
            "problem_grants": self._status_grants(session),
        }

    def create_problem(
        self,
        *,
        session: AgentSessionIdentity,
        problem: str,
    ) -> dict[str, object]:
        if session.general_scope == "none" or not self.access_query.agent_scope_allows(
            session.general_scope,
            "commit",
        ):
            raise AgentGeneralPermissionRequired(required_scope="commit")
        safe_problem = self._require_problem_slug(problem)
        expected_owner = session.username.lower()
        if not safe_problem.startswith(f"{expected_owner}/"):
            raise ValueError(f"new problem must be owned by {expected_owner}")
        if self.workspace_service.known_problem_id(safe_problem) is not None:
            raise FileExistsError("problem already exists")
        self.workspace_service.ensure_problem(safe_problem)
        self.workspace_service.grant_repo_access(
            safe_problem,
            session.username,
            "owner",
        )
        self.workspace_service.ensure_workspace(
            safe_problem,
            session.username,
        )
        self.workspace_service.page_identity(safe_problem, session.username)
        return {"problem": safe_problem}

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
        if row is None or row["user_id"] != int(actor_user_id):
            raise LookupError("access request not found")
        safe_decision = str(decision or "").strip().lower()
        if safe_decision not in {"approve", "deny"}:
            raise ValueError("invalid decision")
        if safe_decision == "deny":
            if row["status"] == "denied":
                return {"status": "denied"}
            if row["status"] != "pending":
                raise ValueError("access request is no longer pending")
            updated = self._store.resolve_pending_request(
                request_id=request_id,
                status="denied",
                resolved_at=now_iso(),
            )
            if updated != 1:
                raise ValueError("access request is no longer pending")
            return {"status": "denied"}
        if row["status"] == "approved":
            return {
                "status": "approved",
                "grant_id": row["grant_id"],
                "granted_scope": row["granted_scope"],
                "expires_at": row["grant_expires_at"] or None,
            }
        safe_scope = self.access_query.canonical_agent_scope(scope)
        ttl_seconds: int | None
        if str(ttl or "").strip().lower() == "forever":
            ttl_seconds = None
        else:
            try:
                ttl_value = int(str(ttl or "").strip())
            except ValueError as exc:
                raise ValueError("invalid ttl") from exc
            if ttl_value not in _ALLOWED_APPROVAL_TTLS:
                raise ValueError("invalid ttl")
            ttl_seconds = ttl_value
        created_at = now_iso()

        def access_check(
            connection: sqlite3.Connection,
            user_id: int,
            problem_id: int,
            granted_scope: str,
        ) -> bool:
            role = self.access_query.problem_role_in_transaction(
                connection,
                problem_id=problem_id,
                user_id=user_id,
            )
            return effective_agent_scope(granted_scope, role) == granted_scope

        result = self._store.approve_access_request(
            actor_user_id=int(actor_user_id),
            request_id=request_id,
            grant_id=f"ag-{secrets.token_hex(8)}",
            granted_scope=safe_scope,
            grant_created_at=created_at,
            grant_expires_at=self._format_expiry(ttl_seconds),
            access_check=access_check,
        )
        if result["outcome"] == "expired":
            raise TimeoutError("access request expired")
        approved = result["request"]
        return {
            "status": "approved",
            "grant_id": approved["grant_id"],
            "granted_scope": approved["granted_scope"],
            "expires_at": approved["grant_expires_at"] or None,
        }

    def list_user_sessions(self, *, user_id: int) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for session_row in self._store.list_user_sessions(int(user_id)):
            items.append(
                {
                    "id": session_row["id"],
                    "agent_name": session_row["agent_name"],
                    "desktop_id": session_row["desktop_id"],
                    "init_ts": session_row["init_ts"],
                    "created_at": session_row["created_at"],
                    "last_seen_at": session_row["last_seen_at"],
                    "general_scope": session_row["general_scope"],
                    "grants": self._settings_grants(
                        session_id=session_row["id"],
                        user_id=session_row["user_id"],
                    ),
                }
            )
        return items

    def set_general_scope(
        self,
        *,
        actor_user_id: int,
        session_id: str,
        general_scope: str,
    ) -> None:
        safe_scope = agent_general_scope(str(general_scope or "").strip())
        updated = self._store.set_general_scope(
            session_id=session_id,
            user_id=int(actor_user_id),
            general_scope=safe_scope,
        )
        if updated != 1:
            raise LookupError("agent session not found")

    def access_request_for_user(
        self,
        *,
        actor_user_id: int,
        request_id: str,
    ) -> dict[str, object]:
        row = self._store.access_request_by_id(request_id)
        if row is None or row["user_id"] != int(actor_user_id):
            raise LookupError("access request not found")
        return dict(row)

    def revoke_grant(self, *, actor_user_id: int, grant_id: str) -> None:
        updated = self._store.revoke_grant(
            grant_id=str(grant_id or ""),
            user_id=int(actor_user_id),
            revoked_at=now_iso(),
        )
        if updated != 1:
            raise LookupError("grant not found")

    def disconnect_session(
        self,
        *,
        actor_user_id: int,
        session_id: str,
    ) -> None:
        row = self._store.session_by_id(session_id)
        if row is None or row["user_id"] != int(actor_user_id):
            raise LookupError("agent session not found")
        deleted = self._store.delete_session_state(
            session_id=str(session_id or ""),
            user_id=int(actor_user_id),
        )
        if deleted["session_count"] <= 0:
            raise LookupError("agent session not found")
