from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_BUILD_REF_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class BuildPaths:
    root: Path
    tests: Path
    ans: Path
    logs: Path
    bin: Path
    export: Path
    statement_preview: Path


class FsManager:
    def __init__(self, artifacts_root: Path, run_root: Path):
        self.artifacts_root = artifacts_root
        self.run_root = run_root

    def compute_build_ref(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def build_paths(self, build_ref: str) -> BuildPaths:
        safe_build_ref = self._normalize_build_ref(build_ref)
        root = self.artifacts_root / "objects" / safe_build_ref[:2] / safe_build_ref
        return BuildPaths(
            root=root,
            tests=root / "tests",
            ans=root / "ans",
            logs=root / "logs",
            bin=root / "bin",
            export=root / "export",
            statement_preview=root / "statement_preview",
        )

    def ensure_build_layout(self, build_ref: str) -> BuildPaths:
        paths = self.build_paths(build_ref)
        paths.root.mkdir(parents=True, exist_ok=True)
        for directory in (paths.tests, paths.ans, paths.logs, paths.bin, paths.export, paths.statement_preview):
            directory.mkdir(parents=True, exist_ok=True)
        return paths

    def prepare_run_root(self, run_id: str) -> Path:
        path = self.resolve_run_root(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_run_root(self, run_id: str) -> Path:
        safe_run_id = self._normalize_token(run_id, field_name="run_id")
        base = self.run_root.resolve()
        target = (base / safe_run_id).resolve()
        if target != base and base not in target.parents:
            raise ValueError("run_id escapes run_root")
        return target

    def latest_ref_path(self, problem: str, kind: str) -> Path:
        safe_kind = self._normalize_token(kind, field_name="kind")
        path = self.artifacts_root / "latest_refs"
        for part in self._normalize_problem(problem):
            path = path / part
        return path / f"{safe_kind}.json"

    def set_latest_ref(self, problem: str, kind: str, build_ref: str) -> None:
        safe_build_ref = self._normalize_build_ref(build_ref)
        path = self.latest_ref_path(problem, kind)
        payload = {"build_ref": safe_build_ref}
        self._atomic_write_json(path, payload)

    def get_latest_ref(self, problem: str, kind: str) -> str | None:
        path = self.latest_ref_path(problem, kind)
        if not path.exists() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        value = str(payload.get("build_ref") or "").strip()
        try:
            return self._normalize_build_ref(value)
        except ValueError:
            return None

    def _normalize_build_ref(self, build_ref: str) -> str:
        token = str(build_ref or "").strip().lower()
        if not _BUILD_REF_RE.fullmatch(token):
            raise ValueError("build_ref must be a 64-char lowercase hex digest")
        return token

    def _normalize_token(self, value: str, *, field_name: str) -> str:
        token = str(value or "").strip()
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError(f"{field_name} has invalid format")
        return token

    def _normalize_problem(self, problem: str) -> tuple[str, ...]:
        raw = str(problem or "").strip().replace("\\", "/")
        parts = tuple(part for part in raw.split("/") if part)
        if not parts:
            raise ValueError("problem must not be empty")
        for part in parts:
            self._normalize_token(part, field_name="problem")
        return parts

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


__all__ = ["BuildPaths", "FsManager"]
