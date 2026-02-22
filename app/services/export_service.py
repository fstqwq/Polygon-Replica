from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso


class ExportService:
    TYPES = {
        "kattis": "kattis.zip",
        "domjudge": "domjudge-legacy-icpc.zip",
        "polygon-standard": "polygon-standard.zip",
        "polygon-full": "polygon-full.zip",
    }

    def __init__(self, db: DB, artifacts_root: Path):
        self.db = db
        self.artifacts_root = artifacts_root

    def create_export(self, problem: str, build_id: str, export_type: str) -> Path:
        if export_type not in self.TYPES:
            raise ValueError("unsupported export type")

        build_root = self.artifacts_root / problem / build_id
        export_dir = build_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)

        base_name = self.TYPES[export_type].replace(".zip", "")
        tmp_root = export_dir / f"tmp-{uuid.uuid4().hex[:8]}"
        tmp_root.mkdir(parents=True, exist_ok=True)

        include_paths = ["manifest.json", "tests", "ans", "statement_preview", "logs"]
        if export_type == "polygon-standard":
            include_paths = ["manifest.json", "statement_preview", "logs"]
        if export_type in {"kattis", "domjudge"}:
            include_paths = ["manifest.json", "tests", "ans"]

        for rel in include_paths:
            src = build_root / rel
            if not src.exists():
                continue
            dst = tmp_root / rel
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        archive = shutil.make_archive(str(export_dir / base_name), "zip", root_dir=tmp_root)
        out = Path(archive)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()

        build_row = self.db.fetch_one("SELECT problem_id,source_commit FROM builds WHERE id=?", [build_id])
        self.db.execute(
            "INSERT INTO exports(id,problem_id,build_id,export_type,filename,sha256,size_bytes,source_commit,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                f"e-{uuid.uuid4().hex[:10]}",
                build_row["problem_id"],
                build_id,
                export_type,
                out.name,
                digest,
                out.stat().st_size,
                build_row["source_commit"],
                now_iso(),
            ],
        )
        shutil.rmtree(tmp_root)
        return out
