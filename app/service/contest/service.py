import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TypedDict

from app.config import ConfigValues
from app.db import DB, now_iso
from app.service.access.policy import access_role, contest_role
from app.service.access.query import AccessQuery
from app.service.contest.model import AgentContestRoster
from app.service.contest.problem_index import normalize_contest_problem_idx
from app.service.contest.property import (
    INSERT_BLANK_PAGE_PROPERTY,
    REQUIRED_CONTEST_PROPERTY_KEYS,
    localized_contest_properties,
    normalize_contest_property_key,
)
from app.service.contest.statement_meta import infer_contest_header_fields
from app.service.contest.statement_source_contract import (
    CONTEST_STATEMENT_OUTPUT_NAME,
    CONTEST_STATEMENT_SHARED_PROTECTED_FILES,
    CONTEST_STATEMENT_SHARED_SCOPE,
    CONTEST_STATEMENT_TEMPLATE_NAME,
)
from app.service.disk.contest_store import ContestDiskStore
from app.service.platform.fs.layout import StorageLayout
from app.service.statement.context import normalize_statement_language


class ContestMemberEntry(TypedDict):
    user_id: int
    username: str
    role: str
    created_at: str
    is_system_admin: int


class ContestMembership(TypedDict):
    user_id: int
    role: str


class ContestProblem(TypedDict):
    contest_problem_id: int
    idx: str
    problem_id: int
    statement_folder: str
    problem_slug: str
    slug_leaf: str
    created_at: str


class ContestAvailableProblem(TypedDict):
    problem_id: int
    problem_slug: str
    slug_leaf: str
    role: str


class ContestProblemEntry(TypedDict):
    idx: str
    problem_id: int
    problem_slug: str
    slug_leaf: str


class ContestContext(TypedDict):
    id: int
    slug: str
    title: str
    owner_user_id: int
    status: str
    source_generation: int
    created_at: str


class ContestProblemLookup(TypedDict):
    id: int
    slug: str


def _required_row_int(
    row: Mapping[str, object], key: str, *, context: str
) -> int:
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{context} {key} must be an integer")
    return value


class ContestStatementSourceFile(TypedDict):
    key: str
    language: str
    source_path: Path


class ContestStatementAttachment(TypedDict):
    key: str
    rel_path: str
    created_at: str


class ContestService:
    _LOCATION_KEY = "location"
    _DATE_KEY = "date"
    _TEXT_SOURCE_SUFFIXES = {
        ".bat",
        ".bib",
        ".bst",
        ".cfg",
        ".cls",
        ".def",
        ".ftl",
        ".ltx",
        ".mp",
        ".sh",
        ".sty",
        ".tex",
        ".txt",
    }

    def __init__(
        self,
        db: DB,
        storage_layout: StorageLayout,
        *,
        access_query: AccessQuery,
        config_values: ConfigValues,
    ):
        self.db = db
        self.storage_layout = storage_layout
        self.access_query = access_query
        self._config_values = config_values
        self._store = ContestDiskStore(db)

    def _contest_idx_from_sequence(self, seq: int) -> str:
        value = max(1, int(seq))
        chars: list[str] = []
        while value > 0:
            value -= 1
            chars.append(chr(ord("A") + (value % 26)))
            value //= 26
        return "".join(reversed(chars))

    def _path_within(self, root: Path, target: Path) -> bool:
        return root == target or root in target.parents

    def _normalize_statement_source_bytes(self, key: str, package_bytes: bytes) -> bytes:
        suffix = Path(str(key).strip()).suffix.lower()
        if suffix not in self._TEXT_SOURCE_SUFFIXES:
            return package_bytes
        text = bytes(package_bytes).decode("utf-8", errors="replace")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.encode("utf-8")

    def statement_source_is_text(self, key: str) -> bool:
        return Path(str(key or "").strip()).suffix.lower() in self._TEXT_SOURCE_SUFFIXES

    def normalize_statement_source_key(
        self,
        *,
        language: str,
        path: str,
        upload_filename: str = "",
        default_filename: str = "",
    ) -> str:
        raw_language = str(language or "").strip()
        safe_language = (
            CONTEST_STATEMENT_SHARED_SCOPE
            if raw_language == CONTEST_STATEMENT_SHARED_SCOPE
            else normalize_statement_language(raw_language)
        )
        if not safe_language:
            raise ValueError("contest statement language is required")
        raw_path = str(path or "").strip().replace("\\", "/")
        upload_name = Path(str(upload_filename or "").strip().replace("\\", "/")).name
        if raw_path.endswith("/") and upload_name:
            raw_path = f"{raw_path}{upload_name}"
        if (not raw_path) and upload_name:
            raw_path = upload_name
        if (not raw_path) and default_filename:
            raw_path = str(default_filename).strip().replace("\\", "/")
        if not raw_path:
            raise ValueError("contest statement source path is required")
        prefix = f"statements/{safe_language}/"
        if raw_path.startswith(prefix):
            rel = raw_path[len(prefix):]
        elif raw_path.startswith("statements/"):
            raise ValueError(f"contest statement source path must be under {prefix}")
        else:
            rel = raw_path
        pure = PurePosixPath(rel)
        if pure.is_absolute():
            raise ValueError("invalid contest statement source path")
        parts: list[str] = []
        for part in pure.parts:
            item = str(part).strip()
            if not item or item == ".":
                continue
            if item == "..":
                raise ValueError("invalid contest statement source path")
            if item.startswith("."):
                raise ValueError("hidden contest statement source path is not allowed")
            parts.append(item)
        if not parts:
            raise ValueError("contest statement source path is required")
        relative_path = PurePosixPath(*parts).as_posix()
        if relative_path == CONTEST_STATEMENT_OUTPUT_NAME:
            raise ValueError(
                f"{CONTEST_STATEMENT_OUTPUT_NAME} is a rendered Contest statement file"
            )
        if (
            safe_language == CONTEST_STATEMENT_SHARED_SCOPE
            and relative_path in CONTEST_STATEMENT_SHARED_PROTECTED_FILES
        ):
            raise ValueError(
                "shared Contest statement files cannot replace "
                f"{CONTEST_STATEMENT_TEMPLATE_NAME} or olymp.sty"
            )
        return f"{prefix}{relative_path}"

    def _normalize_existing_statement_source_key(self, key: str) -> str:
        parts = PurePosixPath(str(key or "").strip().replace("\\", "/")).parts
        if len(parts) >= 3 and parts[0] == "statements":
            return self.normalize_statement_source_key(language=parts[1], path=str(key), default_filename="")
        raise ValueError("contest statement source must include its language path")

    def write_statement_source_file(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        actor_user_id: int,
        key: str,
        package_bytes: bytes,
    ) -> str:
        safe_key = self._normalize_existing_statement_source_key(key)
        root = self.contest_source_root(contest_slug)
        target = (root / safe_key).resolve()
        if not self._path_within(root, target):
            raise ValueError("invalid contest source file path")
        if target.exists() and target.is_dir():
            raise ValueError("contest source target must be a file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._normalize_statement_source_bytes(safe_key, bytes(package_bytes)))
        self._store.upsert_attachment_row(
            contest_id=int(contest_id),
            key=safe_key,
            rel_path=safe_key,
            created_by_user_id=int(actor_user_id),
            created_at=now_iso(),
        )
        self._store.bump_source_generation(int(contest_id))
        return safe_key

    def delete_statement_source_file(self, *, contest_id: int, contest_slug: str, key: str) -> str:
        safe_key = self._normalize_existing_statement_source_key(key)
        target = self.statement_file_path(contest_slug, safe_key)
        if target.exists():
            if target.is_dir():
                raise ValueError("contest source target must be a file path")
            target.unlink()
            root = self.contest_source_root(contest_slug)
            current = target.parent
            while current != root and self._path_within(root, current):
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent
            try:
                root.rmdir()
            except OSError:
                pass
        self._store.delete_attachment_row(int(contest_id), safe_key)
        self._store.bump_source_generation(int(contest_id))
        return safe_key

    def delete_statement_language_sources(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        language: str,
    ) -> int:
        safe_language = normalize_statement_language(language)
        if not safe_language:
            raise ValueError("contest statement language is required")
        prefix = f"statements/{safe_language}/"
        keys = [
            self._normalize_existing_statement_source_key(str(row["rel_path"]))
            for row in self.statement_attachment_rows(int(contest_id))
            if str(row["rel_path"]).startswith(prefix)
        ]
        if not keys:
            return 0

        root = self.contest_source_root(contest_slug)
        language_root = (root / "statements" / safe_language).resolve()
        if not self._path_within(root, language_root):
            raise ValueError("invalid contest statement language path")
        if language_root.exists():
            if not language_root.is_dir() or language_root.is_symlink():
                raise ValueError("contest statement language path must be a directory")
            shutil.rmtree(language_root)
        return self._store.delete_attachment_rows_and_bump(int(contest_id), keys)

    def contest_sources_base(self) -> Path:
        return self.storage_layout.contest_source_root.resolve()

    def contest_source_root(self, contest_slug: str) -> Path:
        base = self.contest_sources_base()
        root = self.storage_layout.contest_source(str(contest_slug).strip())
        if not self._path_within(base, root):
            raise ValueError("invalid contest source path")
        return root

    def package_download_root(self, contest_slug: str, operation_id: str) -> Path:
        base = self.storage_layout.contest_package_download_root.resolve()
        root = self.storage_layout.contest_package_download(
            str(contest_slug).strip(),
            str(operation_id).strip(),
        )
        if not self._path_within(base, root):
            raise ValueError("invalid Contest package download path")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def user_contests_overview(self, user_id: int, *, limit: int) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        if self.access_query.is_system_admin(user_id):
            rows = self._store.all_contest_rows(int(user_id), limit=max(1, int(limit)))
        else:
            rows = self._store.user_contest_rows(int(user_id), limit=max(1, int(limit)))
        for row in rows:
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
                    "role": access_role(str(row["role"])),
                    "problem_count": problem_count,
                    "dirty_problem_count": dirty_problem_count,
                    "has_dirty": dirty_problem_count > 0,
                }
            )
        return items

    def contest_slug_exists(self, contest_slug: str) -> bool:
        return self._store.contest_slug_exists(contest_slug)

    def delete_contest(self, contest_slug: str) -> None:
        safe_slug = str(contest_slug).strip()
        if not safe_slug:
            return
        row = self._store.contest_context_row(safe_slug)
        if row is None:
            return
        contest_id = int(row["id"])
        self._store.delete_contest(contest_id)
        source_root = (self.contest_sources_base() / safe_slug).resolve()
        if source_root.exists():
            shutil.rmtree(source_root, ignore_errors=True)

    def create_contest_with_owner(self, *, slug: str, title: str, owner_user_id: int) -> int:
        return self._store.create_contest_with_owner(
            slug=slug,
            title=title,
            owner_user_id=int(owner_user_id),
            created_at=now_iso(),
        )

    def add_problem(self, contest_id: int, idx: str, problem_id: int, added_by_user_id: int) -> int:
        contest_problem_id = self._store.add_problem(
            int(contest_id),
            normalize_contest_problem_idx(idx),
            int(problem_id),
            int(added_by_user_id),
            now_iso(),
            max_problems=self._config_values.integer("CONTEST_MAX_PROBLEMS"),
        )
        self._store.bump_source_generation(int(contest_id))
        return contest_problem_id

    def contest_context(self, contest_slug: str) -> ContestContext | None:
        row = self._store.contest_context_row(contest_slug)
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "owner_user_id": int(row["owner_user_id"]),
            "status": str(row["status"]),
            "source_generation": int(row["source_generation"]),
            "created_at": str(row["created_at"]),
        }

    def agent_roster(self, contest_slug: str) -> AgentContestRoster | None:
        return self._store.agent_roster(str(contest_slug or ""))

    def owner_count(self, contest_id: int) -> int:
        return self._store.owner_count(int(contest_id))

    def member_count(self, contest_id: int) -> int:
        return self._store.member_count(int(contest_id))

    def member_entries(self, contest_id: int) -> list[ContestMemberEntry]:
        result: list[ContestMemberEntry] = []
        for row in self._store.member_entries(int(contest_id)):
            result.append(
                {
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"]),
                    "role": contest_role(str(row["role"])),
                    "created_at": str(row["created_at"]),
                    "is_system_admin": int(row["is_system_admin"] or 0),
                }
            )
        return result

    def grant_member_role(self, contest_id: int, username: str, role: str) -> bool:
        safe_role = contest_role(role)
        if safe_role == "owner":
            raise ValueError("owner access is fixed and cannot be transferred")
        user_id = self._store.user_id_by_username(username)
        if user_id is None:
            return False
        self._store.grant_member_role(int(contest_id), user_id, safe_role, now_iso())
        return True

    def membership_for_username(self, contest_id: int, username: str) -> ContestMembership | None:
        row = self._store.membership_for_username(int(contest_id), username)
        if row is None:
            return None
        return {
            "user_id": int(row["user_id"]),
            "role": contest_role(str(row["role"])),
        }

    def revoke_member(self, contest_id: int, user_id: int) -> None:
        self._store.revoke_member(int(contest_id), int(user_id))

    def properties_map(self, contest_id: int) -> dict[str, str]:
        return self._store.property_map(int(contest_id))

    def localized_properties_map(
        self,
        contest_id: int,
        language: str,
    ) -> dict[str, str]:
        return localized_contest_properties(
            self.properties_map(int(contest_id)),
            language,
        )

    def overview_properties_map(self, contest_id: int, contest_slug: str) -> dict[str, str]:
        result = self.properties_map(int(contest_id))
        if result.get(self._LOCATION_KEY) and result.get(self._DATE_KEY):
            return result
        inferred = self._infer_statement_header_fields_for_contest(int(contest_id), contest_slug)
        if (not result.get(self._LOCATION_KEY)) and inferred["location"]:
            result[self._LOCATION_KEY] = inferred["location"]
        if (not result.get(self._DATE_KEY)) and inferred["date"]:
            result[self._DATE_KEY] = inferred["date"]
        return result

    def localized_overview_properties_map(
        self,
        contest_id: int,
        contest_slug: str,
        language: str,
    ) -> dict[str, str]:
        result = localized_contest_properties(
            self.properties_map(int(contest_id)),
            language,
        )
        if result.get(self._LOCATION_KEY) and result.get(self._DATE_KEY):
            return result
        inferred = self._infer_statement_header_fields_for_contest(
            int(contest_id), contest_slug
        )
        if not result.get(self._LOCATION_KEY) and inferred["location"]:
            result[self._LOCATION_KEY] = inferred["location"]
        if not result.get(self._DATE_KEY) and inferred["date"]:
            result[self._DATE_KEY] = inferred["date"]
        return result

    def set_properties(
        self,
        contest_id: int,
        actor_user_id: int,
        values: Mapping[str, object],
    ) -> bool:
        del actor_user_id
        normalized: dict[str, str | None] = {}
        for key, value in values.items():
            safe_key = normalize_contest_property_key(key)
            if value is None:
                if safe_key in REQUIRED_CONTEST_PROPERTY_KEYS:
                    raise ValueError(f"contest property cannot be deleted: {safe_key}")
                normalized[safe_key] = None
                continue
            if safe_key == INSERT_BLANK_PAGE_PROPERTY:
                if isinstance(value, bool):
                    normalized[safe_key] = "true" if value else "false"
                    continue
                safe_boolean = str(value).strip().lower()
                if safe_boolean not in {"true", "false"}:
                    raise ValueError(
                        "insertBlankPage must be true or false"
                    )
                normalized[safe_key] = safe_boolean
                continue
            safe_value = str(value).strip()
            if safe_key == "title" and not safe_value:
                raise ValueError("contest title is required")
            normalized[safe_key] = safe_value or None
        return self._store.set_property_values(int(contest_id), normalized)

    def replace_statement_sources(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        actor_user_id: int,
        files: list[ContestStatementSourceFile],
    ) -> None:
        normalized_files: list[tuple[str, Path]] = []
        for row in files:
            key = self._normalize_existing_statement_source_key(
                str(row["key"]).strip()
            )
            source_path = Path(row["source_path"])
            if not source_path.is_file() or source_path.is_symlink():
                raise ValueError(
                    f"contest statement source is unavailable: {key}"
                )
            normalized_files.append((key, source_path))

        root = self.contest_source_root(contest_slug)
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        attachment_rows: list[tuple[str, str]] = []
        safe_root = root.resolve()
        for key, source_path in normalized_files:
            rel_path = Path(key)
            target = (root / rel_path).resolve()
            if safe_root not in target.parents:
                raise ValueError(f"invalid contest source path: {key}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if self.statement_source_is_text(key):
                self._copy_normalized_text_file(source_path, target)
            else:
                shutil.copyfile(source_path, target)
            attachment_rows.append((key, key))
        self._store.replace_attachment_rows(
            contest_id=int(contest_id),
            created_by_user_id=int(actor_user_id),
            created_at=now_iso(),
            rows=attachment_rows,
        )
        self._store.bump_source_generation(int(contest_id))

    @staticmethod
    def _copy_normalized_text_file(source: Path, target: Path) -> None:
        pending_cr = False
        with source.open("rb") as input_handle, target.open("wb") as output_handle:
            while chunk := input_handle.read(1024 * 1024):
                if pending_cr:
                    chunk = b"\r" + chunk
                    pending_cr = False
                if chunk.endswith(b"\r"):
                    chunk = chunk[:-1]
                    pending_cr = True
                output_handle.write(chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            if pending_cr:
                output_handle.write(b"\n")

    def statement_attachment_rows(self, contest_id: int) -> list[ContestStatementAttachment]:
        result: list[ContestStatementAttachment] = []
        for row in self._store.attachment_rows(int(contest_id)):
            result.append(
                {
                    "key": str(row["key"]),
                    "rel_path": str(row["rel_path"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def statement_file_path(self, contest_slug: str, rel_path: str) -> Path:
        root = self.contest_source_root(contest_slug)
        target = (root / str(rel_path).strip()).resolve()
        if not self._path_within(root, target):
            raise ValueError("invalid contest source file path")
        return target

    def _infer_statement_header_fields_for_contest(
        self,
        contest_id: int,
        contest_slug: str,
    ) -> dict[str, str]:
        statement_paths = sorted(
            str(row["rel_path"])
            for row in self.statement_attachment_rows(int(contest_id))
            if str(row["rel_path"]).endswith(
                f"/{CONTEST_STATEMENT_TEMPLATE_NAME}"
            )
        )
        english_path = f"statements/english/{CONTEST_STATEMENT_TEMPLATE_NAME}"
        candidate_rel_paths = (
            [english_path, *[path for path in statement_paths if path != english_path]]
            if english_path in statement_paths
            else statement_paths
        )
        for rel_path in candidate_rel_paths:
            try:
                source_path = self.statement_file_path(contest_slug, rel_path)
                text = source_path.read_text(encoding="utf-8")
            except Exception:
                continue
            inferred = infer_contest_header_fields(text)
            if inferred["title"] or inferred["location"] or inferred["date"]:
                return inferred
        return {"title": "", "location": "", "date": ""}

    def statement_problem_source_folders(self, contest_id: int) -> dict[int, str]:
        return {
            int(row["problem_id"]): str(row["statement_folder"])
            for row in self._store.contest_problem_rows(int(contest_id))
            if str(row["statement_folder"])
        }

    def set_statement_problem_source_folders(
        self,
        contest_id: int,
        actor_user_id: int,
        source_folders: dict[int, str],
    ) -> None:
        del actor_user_id
        payload = {
            int(problem_id): str(folder).strip()
            for problem_id, folder in source_folders.items()
            if int(problem_id) > 0 and str(folder).strip()
        }
        self._store.set_problem_statement_folders(int(contest_id), payload)
        self._store.bump_source_generation(int(contest_id))

    def contest_problems(self, contest_id: int) -> list[ContestProblem]:
        result: list[ContestProblem] = []
        for row in self._store.contest_problem_rows(int(contest_id)):
            result.append(
                {
                    "contest_problem_id": int(row["contest_problem_id"]),
                    "idx": str(row["idx"]),
                    "problem_id": row["problem_id"],
                    "statement_folder": str(row["statement_folder"]),
                    "problem_slug": str(row["problem_slug"]),
                    "slug_leaf": str(row["slug_leaf"]),
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
                    "slug_leaf": row["slug_leaf"],
                }
            )
        return result

    def available_problems(self, contest_id: int, user_id: int, *, limit: int, query: str) -> list[ContestAvailableProblem]:
        filter_text = str(query).strip().lower()
        result: list[ContestAvailableProblem] = []
        for row in self.access_query.directly_writable_problem_rows_excluding_contest(
            contest_id,
            user_id,
            limit=max(1, int(limit)),
        ):
            slug = str(row["problem_slug"])
            slug_leaf = slug.rsplit("/", 1)[-1]
            if filter_text and filter_text not in f"{slug} {slug_leaf}".lower():
                continue
            result.append(
                {
                    "problem_id": _required_row_int(
                        row, "problem_id", context="available problem"
                    ),
                    "problem_slug": slug,
                    "slug_leaf": slug_leaf,
                    "role": access_role(str(row["role"])),
                }
            )
        return result

    def problem_count(self, contest_id: int) -> int:
        return self._store.problem_count(int(contest_id))

    def problem_by_slug(self, slug: str) -> ContestProblemLookup | None:
        row = self._store.problem_by_slug(slug)
        if row is None:
            return None
        return {"id": int(row["id"]), "slug": str(row["slug"])}

    def contest_has_problem(self, contest_id: int, problem_id: int) -> bool:
        return self._store.contest_has_problem(int(contest_id), int(problem_id))

    def next_problem_index(self, contest_id: int) -> str:
        used = set(self._store.used_problem_indices(int(contest_id)))
        seq = 1
        while seq < 100000:
            token = self._contest_idx_from_sequence(seq)
            if token not in used:
                return token
            seq += 1
        raise RuntimeError("unable to allocate contest problem index")

    def remove_problem(self, contest_id: int, problem_id: int) -> bool:
        removed = self._store.remove_problem(int(contest_id), int(problem_id))
        if removed:
            self._store.bump_source_generation(int(contest_id))
        return removed

    def remove_problems(self, contest_id: int, problem_ids: list[int]) -> int:
        removed = self._store.remove_problems(int(contest_id), problem_ids)
        if removed:
            self._store.bump_source_generation(int(contest_id))
        return removed

    def set_problem_indices(self, contest_id: int, pairs: list[tuple[int, str]]) -> bool:
        canonical_pairs = [
            (int(contest_problem_id), normalize_contest_problem_idx(idx))
            for contest_problem_id, idx in pairs
        ]
        return self._store.set_problem_indices(int(contest_id), canonical_pairs)

    def selected_problems(self, contest_id: int, problem_ids: list[int]) -> list[ContestProblemEntry]:
        result: list[ContestProblemEntry] = []
        for row in self._store.selected_problem_rows(int(contest_id), problem_ids):
            result.append(
                {
                    "idx": str(row["idx"]),
                    "problem_id": int(row["problem_id"]),
                    "problem_slug": str(row["problem_slug"]),
                    "slug_leaf": str(row["slug_leaf"]),
                }
            )
        return result
