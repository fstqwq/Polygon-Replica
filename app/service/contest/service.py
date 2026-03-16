from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import TypedDict

from app.db import DB, now_iso
from app.service.disk.contest_store import ContestDiskStore
from app.service.disk.preview_store import PreviewStore
from app.service.disk.verification_store import VerificationStore
from app.service.platform.hashing import sha256_file
from app.service.platform.process import is_canonical_artifact_id
from app.setting import Settings


class ContestAccessContext(TypedDict):
    role: str
    can_read: bool
    can_write: bool
    can_manage: bool
    read_block_reason: str
    write_block_reason: str
    manage_block_reason: str


class ContestMemberEntry(TypedDict):
    username: str
    role: str
    created_at: str


class ContestMembership(TypedDict):
    user_id: int
    role: str


class ContestProblem(TypedDict):
    contest_problem_id: int
    idx: str
    problem_id: int
    problem_slug: str
    problem_name: str
    created_at: str


class ContestAvailableProblem(TypedDict):
    problem_id: int
    problem_slug: str
    problem_name: str
    role: str


class ContestProblemEntry(TypedDict):
    idx: str
    problem_id: int
    problem_slug: str
    problem_name: str


class ContestContext(TypedDict):
    id: int
    slug: str
    title: str
    owner_user_id: int
    created_at: str


class ContestProblemLookup(TypedDict):
    id: int
    slug: str
    name: str


class ContestJob(TypedDict):
    id: str
    job_type: str
    status: str
    summary: dict[str, object]
    created_at: str
    finished_at: str


class ContestArtifact(TypedDict):
    id: str
    job_id: str
    artifact_type: str
    filename: str
    artifact_path: str
    size_bytes: int
    created_at: str
    downloadable: bool


class ContestPreviewResult(TypedDict):
    status: str
    summary: dict[str, object]
    artifact_path: str


class ContestVerificationStage(TypedDict):
    id: str
    status: str
    source_commit: str
    source_ref: str
    summary: dict[str, object]
    created_at: str
    finished_at: str


class ContestService:
    _ARTIFACT_BUCKET = "__contests__"
    _ACCESS_ROLES = {"owner", "write", "read"}

    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings
        self._store = ContestDiskStore(db)
        self._preview_store = PreviewStore(db)
        self._verification_store = VerificationStore(db)

    def _normalize_role(self, raw_role: str | None) -> str:
        if raw_role in self._ACCESS_ROLES:
            return raw_role
        return "read"

    def _parse_summary(self, raw_summary: str) -> dict[str, object]:
        text = str(raw_summary).strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    def _property_text(self, raw_json: str) -> str:
        text = str(raw_json).strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except Exception:
            return text
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        return str(payload)

    def _contest_idx_label(self, seq: int) -> str:
        value = max(1, int(seq))
        chars: list[str] = []
        while value > 0:
            value -= 1
            chars.append(chr(ord("A") + (value % 26)))
            value //= 26
        return "".join(reversed(chars))

    def _path_within(self, root: Path, target: Path) -> bool:
        return root == target or root in target.parents

    def _job_payload(self, row: dict[str, object]) -> ContestJob:
        return {
            "id": str(row["id"]),
            "job_type": str(row["job_type"]),
            "status": str(row["status"]),
            "summary": self._parse_summary(str(row["summary_json"])),
            "created_at": str(row["created_at"]),
            "finished_at": str(row["finished_at"]),
        }

    def artifacts_base(self) -> Path:
        base = (self.settings.artifacts_root / self._ARTIFACT_BUCKET).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return base

    def job_root(self, contest_slug: str, job_id: str) -> Path:
        safe_job_id = str(job_id).strip()
        if not is_canonical_artifact_id(safe_job_id):
            raise ValueError("invalid contest job id")
        base = self.artifacts_base()
        root = (base / str(contest_slug).strip() / safe_job_id).resolve()
        if not self._path_within(base, root):
            raise ValueError("invalid contest artifact path")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def user_contests_overview(self, user_id: int, *, limit: int) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for row in self._store.user_contest_rows(int(user_id), limit=max(1, int(limit))):
            problem_count = max(0, int(row["problem_count"]))
            dirty_problem_count = max(0, int(row["dirty_problem_count"]))
            items.append(
                {
                    "id": int(row["id"]),
                    "slug": str(row["slug"]),
                    "title": str(row["title"]),
                    "owner_user_id": int(row["owner_user_id"]),
                    "created_at": str(row["created_at"]),
                    "last_updated_at": str(row["last_updated_at"]),
                    "role": self._normalize_role(str(row["role"])),
                    "problem_count": problem_count,
                    "problem_slugs_preview": str(row["problem_slugs_preview"]),
                    "problem_preview_truncated": problem_count > 5,
                    "dirty_problem_count": dirty_problem_count,
                    "has_dirty": dirty_problem_count > 0,
                }
            )
        return items

    def contest_slug_exists(self, contest_slug: str) -> bool:
        return self._store.contest_slug_exists(contest_slug)

    def create_contest_with_owner(self, *, slug: str, title: str, owner_user_id: int) -> int:
        return self._store.create_contest_with_owner(
            slug=slug,
            title=title,
            owner_user_id=int(owner_user_id),
            created_at=now_iso(),
        )

    def add_problem(self, contest_id: int, idx: str, problem_id: int, added_by_user_id: int) -> None:
        self._store.add_problem(
            int(contest_id),
            idx,
            int(problem_id),
            int(added_by_user_id),
            now_iso(),
        )

    def contest_context(self, contest_slug: str) -> ContestContext | None:
        row = self._store.contest_context_row(contest_slug)
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "owner_user_id": int(row["owner_user_id"]),
            "created_at": str(row["created_at"]),
        }

    def access_context(self, contest_id: int, user_id: int) -> ContestAccessContext:
        role = self._store.contest_role(int(contest_id), int(user_id))
        if role is None:
            return {
                "role": "none",
                "can_read": False,
                "can_write": False,
                "can_manage": False,
                "read_block_reason": "you do not have access to this contest",
                "write_block_reason": "write access required",
                "manage_block_reason": "owner access required",
            }
        safe_role = self._normalize_role(role)
        can_write = safe_role in {"owner", "write"}
        return {
            "role": safe_role,
            "can_read": True,
            "can_write": can_write,
            "can_manage": safe_role == "owner",
            "read_block_reason": "",
            "write_block_reason": "" if can_write else "read-only access",
            "manage_block_reason": "" if safe_role == "owner" else "owner access required",
        }

    def owner_count(self, contest_id: int) -> int:
        return self._store.owner_count(int(contest_id))

    def member_count(self, contest_id: int) -> int:
        return self._store.member_count(int(contest_id))

    def member_entries(self, contest_id: int) -> list[ContestMemberEntry]:
        result: list[ContestMemberEntry] = []
        for row in self._store.member_entries(int(contest_id)):
            result.append(
                {
                    "username": str(row["username"]),
                    "role": self._normalize_role(str(row["role"])),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def grant_member_role(self, contest_id: int, username: str, role: str) -> bool:
        user_id = self._store.user_id_by_username(username)
        if user_id is None:
            return False
        self._store.grant_member_role(int(contest_id), user_id, role, now_iso())
        return True

    def membership_for_username(self, contest_id: int, username: str) -> ContestMembership | None:
        row = self._store.membership_for_username(int(contest_id), username)
        if row is None:
            return None
        return {
            "user_id": int(row["user_id"]),
            "role": self._normalize_role(str(row["role"])),
        }

    def revoke_member(self, contest_id: int, user_id: int) -> None:
        self._store.revoke_member(int(contest_id), int(user_id))

    def properties_map(self, contest_id: int) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in self._store.property_rows(int(contest_id)):
            key = str(row["key"]).strip()
            if key:
                result[key] = self._property_text(str(row["value_json"]))
        return result

    def update_title(self, contest_id: int, title: str) -> None:
        self._store.update_title(int(contest_id), title)

    def upsert_property(self, contest_id: int, actor_user_id: int, key: str, value: str) -> None:
        self._store.upsert_property(int(contest_id), int(actor_user_id), key, value, now_iso())

    def contest_problems(self, contest_id: int) -> list[ContestProblem]:
        result: list[ContestProblem] = []
        for row in self._store.contest_problem_rows(int(contest_id)):
            result.append(
                {
                    "contest_problem_id": int(row["contest_problem_id"]),
                    "idx": str(row["idx"]),
                    "problem_id": int(row["problem_id"]),
                    "problem_slug": str(row["problem_slug"]),
                    "problem_name": str(row["problem_name"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def contest_problem_entries(self, contest_id: int) -> list[ContestProblemEntry]:
        result: list[ContestProblemEntry] = []
        for row in self.contest_problems(int(contest_id)):
            result.append(
                {
                    "idx": row["idx"],
                    "problem_id": row["problem_id"],
                    "problem_slug": row["problem_slug"],
                    "problem_name": row["problem_name"],
                }
            )
        return result

    def available_problems(self, contest_id: int, user_id: int, *, limit: int, query: str) -> list[ContestAvailableProblem]:
        filter_text = str(query).strip().lower()
        result: list[ContestAvailableProblem] = []
        for row in self._store.available_problem_rows(int(contest_id), int(user_id), limit=max(1, int(limit))):
            slug = str(row["problem_slug"])
            name = str(row["problem_name"])
            if filter_text and filter_text not in f"{slug} {name}".lower():
                continue
            result.append(
                {
                    "problem_id": int(row["problem_id"]),
                    "problem_slug": slug,
                    "problem_name": name,
                    "role": self._normalize_role(str(row["role"])),
                }
            )
        return result

    def problem_count(self, contest_id: int) -> int:
        return self._store.problem_count(int(contest_id))

    def problem_by_slug(self, slug: str) -> ContestProblemLookup | None:
        row = self._store.problem_by_slug(slug)
        if row is None:
            return None
        return {"id": int(row["id"]), "slug": str(row["slug"]), "name": str(row["name"])}

    def contest_has_problem(self, contest_id: int, problem_id: int) -> bool:
        return self._store.contest_has_problem(int(contest_id), int(problem_id))

    def next_problem_index(self, contest_id: int) -> str:
        used = set(self._store.used_problem_indices(int(contest_id)))
        seq = 1
        while seq < 100000:
            token = self._contest_idx_label(seq)
            if token not in used:
                return token
            seq += 1
        raise RuntimeError("unable to allocate contest problem index")

    def remove_problem(self, contest_id: int, problem_id: int) -> bool:
        return self._store.remove_problem(int(contest_id), int(problem_id))

    def remove_problems(self, contest_id: int, problem_ids: list[int]) -> int:
        return self._store.remove_problems(int(contest_id), problem_ids)

    def reorder_problem_indices(self, contest_id: int, pairs: list[tuple[int, str]]) -> bool:
        return self._store.reorder_problem_indices(int(contest_id), pairs)

    def renumber_problem_indices(self, contest_id: int) -> None:
        self._store.renumber_problem_indices(int(contest_id))

    def selected_problems(self, contest_id: int, problem_ids: list[int]) -> list[ContestProblemEntry]:
        result: list[ContestProblemEntry] = []
        for row in self._store.selected_problem_rows(int(contest_id), problem_ids):
            result.append(
                {
                    "idx": str(row["idx"]),
                    "problem_id": int(row["problem_id"]),
                    "problem_slug": str(row["problem_slug"]),
                    "problem_name": str(row["problem_name"]),
                }
            )
        return result

    def create_job(
        self,
        contest_id: int,
        actor_user_id: int,
        job_type: str,
        status: str,
        summary: dict[str, object],
        *,
        finished_at: str | None = None,
    ) -> str:
        job_id = f"cj-{secrets.token_hex(6)}"
        created_at = now_iso()
        safe_status = str(status).strip().lower() or "failed"
        resolved_finished_at = finished_at
        if resolved_finished_at is None and safe_status not in {"running", "queued"}:
            resolved_finished_at = created_at
        self._store.insert_job(
            job_id=job_id,
            contest_id=int(contest_id),
            actor_user_id=int(actor_user_id),
            job_type=str(job_type).strip(),
            status=safe_status,
            summary=summary,
            created_at=created_at,
            finished_at=resolved_finished_at,
        )
        return job_id

    def update_job(
        self,
        contest_id: int,
        job_id: str,
        status: str,
        summary: dict[str, object],
        *,
        finished: bool = True,
    ) -> None:
        safe_job_id = str(job_id).strip()
        if not safe_job_id:
            return
        self._store.update_job(
            contest_id=int(contest_id),
            job_id=safe_job_id,
            status=str(status).strip().lower() or "failed",
            summary=summary,
            finished_at=now_iso() if finished else None,
        )

    def load_job(self, contest_id: int, job_id: str) -> ContestJob | None:
        safe_job_id = str(job_id).strip()
        if not safe_job_id:
            return None
        row = self._store.job_row(int(contest_id), safe_job_id)
        if row is None:
            return None
        return self._job_payload(row)

    def latest_job(self, contest_id: int) -> ContestJob | None:
        row = self._store.latest_job_row(int(contest_id))
        if row is None:
            return None
        return self._job_payload(row)

    def list_jobs(self, contest_id: int, *, limit: int) -> list[ContestJob]:
        return [self._job_payload(row) for row in self._store.job_rows(int(contest_id), limit=max(1, int(limit)))]

    def job_status(self, contest_id: int, job_id: str) -> str:
        return str(self._store.job_status(int(contest_id), str(job_id).strip())).strip().lower()

    def running_job_id(self, contest_id: int, job_type: str) -> str:
        return self._store.running_job_id(int(contest_id), str(job_type).strip())

    def record_artifact(
        self,
        *,
        contest_id: int,
        job_id: str,
        artifact_type: str,
        filename: str,
        artifact_path: Path,
    ) -> str:
        safe_filename = Path(str(filename).strip() or artifact_path.name).name
        resolved = artifact_path.resolve()
        base = self.artifacts_base()
        if not self._path_within(base, resolved):
            raise ValueError("invalid contest artifact path")
        if not resolved.exists() or not resolved.is_file() or resolved.is_symlink():
            raise ValueError("contest artifact file not found")
        artifact_id = f"ca-{secrets.token_hex(6)}"
        self._store.insert_artifact(
            artifact_id=artifact_id,
            contest_id=int(contest_id),
            job_id=str(job_id).strip(),
            artifact_type=str(artifact_type).strip(),
            filename=safe_filename,
            artifact_path=str(resolved),
            sha256=sha256_file(resolved),
            size_bytes=int(resolved.stat().st_size),
            created_at=now_iso(),
        )
        return artifact_id

    def list_artifacts(self, contest_id: int, *, limit: int) -> list[ContestArtifact]:
        base = self.artifacts_base()
        result: list[ContestArtifact] = []
        for row in self._store.artifact_rows(int(contest_id), limit=max(1, int(limit))):
            artifact_path = Path(str(row["artifact_path"])).resolve()
            downloadable = (
                is_canonical_artifact_id(str(row["id"]))
                and self._path_within(base, artifact_path)
                and artifact_path.exists()
                and artifact_path.is_file()
                and (not artifact_path.is_symlink())
            )
            result.append(
                {
                    "id": str(row["id"]),
                    "job_id": str(row["job_id"]),
                    "artifact_type": str(row["artifact_type"]),
                    "filename": str(row["filename"]),
                    "artifact_path": str(row["artifact_path"]),
                    "size_bytes": int(row["size_bytes"]),
                    "created_at": str(row["created_at"]),
                    "downloadable": downloadable,
                }
            )
        return result

    def artifact_download(self, contest_id: int, artifact_id: str) -> tuple[Path, str] | None:
        safe_artifact_id = str(artifact_id).strip()
        if not is_canonical_artifact_id(safe_artifact_id):
            return None
        row = self._store.artifact_row(int(contest_id), safe_artifact_id)
        if row is None:
            return None
        file_path = Path(str(row["artifact_path"])).resolve()
        base = self.artifacts_base()
        if not self._path_within(base, file_path):
            return None
        if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
            return None
        filename = Path(str(row["filename"])).name or file_path.name
        return (file_path, filename)

    def preview_result(self, preview_id: str) -> ContestPreviewResult | None:
        row = self._preview_store.get_preview_row(str(preview_id).strip())
        if row is None:
            return None
        return {
            "status": str(row["status"]).strip().lower(),
            "summary": self._parse_summary(str(row["summary_json"])),
            "artifact_path": str(row["artifact_path"]),
        }

    def verification_stage(self, problem_id: int, workspace_id: int, verification_id: str) -> ContestVerificationStage | None:
        row = self._verification_store.workspace_stage_row(int(problem_id), int(workspace_id), verification_id)
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]).strip().lower(),
            "source_commit": str(row["source_commit"]),
            "source_ref": str(row["source_ref"]),
            "summary": self._parse_summary(str(row["summary_json"])),
            "created_at": str(row["created_at"]),
            "finished_at": str(row["finished_at"]),
        }
